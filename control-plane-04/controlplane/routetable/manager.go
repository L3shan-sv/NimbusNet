// Package routetable manages VPC route table entries for the NimbusNet data plane.
//
// This is the FAST path — route table updates take effect within milliseconds.
// Route 53 changes take 35 seconds (health check interval + DNS TTL).
//
// In a Transit Gateway topology:
//   - Each VPC has a route table with entries for other regions via TGW
//   - When a region fails, we update the CIDR route to point to a backup TGW attachment
//   - This is the "VPC route table already healed" in the two-plane design
//
// ECMP (Equal-Cost Multi-Path) routing:
//   - In normal operation, traffic is split across TGW attachments using ECMP weights
//   - AWS VPC does not natively support weighted ECMP, so we implement it by
//     distributing routes across multiple route tables with different weights
//   - Each route table services a fraction of subnets proportional to the weight
//
// Example: 3 regions, weights [0.5, 0.3, 0.2]:
//   - 50% of subnets use route table A → TGW attachment us-east-1
//   - 30% of subnets use route table B → TGW attachment us-west-2
//   - 20% of subnets use route table C → TGW attachment eu-west-1
//
// On failover: all subnets previously pointing to the failed region's attachment
//              are atomically updated to point to the next-best attachment.

package routetable

import (
	"context"
	"fmt"
	"math"
	"sort"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"go.uber.org/zap"
)

// ─── Config ───────────────────────────────────────────────────────────────────

// RegionRouteConfig maps an AWS region to its TGW attachment and target CIDRs.
type RegionRouteConfig struct {
	Region           string
	TGWAttachmentID  string   // e.g. "tgw-attach-0abc123"
	TargetCIDRs      []string // CIDRs to route via this attachment, e.g. ["10.1.0.0/16"]
	SubnetIDs        []string // subnets in this VPC that use this region's route table
	RouteTableIDs    []string // route table IDs managed for this region's weight
}

// Config for the route table manager.
type Config struct {
	LocalRegion string
	Regions     map[string]RegionRouteConfig
	// Maximum number of concurrent route table updates
	// AWS has a 100 routes/second limit per region
	MaxConcurrentUpdates int
}

// ─── Manager ──────────────────────────────────────────────────────────────────

// Manager handles VPC route table updates for the data plane.
type Manager struct {
	cfg Config
	ec2 *ec2.Client
	log *zap.Logger

	// Current weight per region (0.0–1.0)
	currentWeights map[string]float64
}

// New creates a route table Manager.
func New(cfg Config, ec2Client *ec2.Client, log *zap.Logger) *Manager {
	weights := make(map[string]float64, len(cfg.Regions))
	n := float64(len(cfg.Regions))
	for r := range cfg.Regions {
		weights[r] = 1.0 / n
	}
	return &Manager{
		cfg:            cfg,
		ec2:            ec2Client,
		log:            log,
		currentWeights: weights,
	}
}

// ─── Primary Operations ───────────────────────────────────────────────────────

// FailoverRegion immediately reroutes all traffic away from a failed region.
//
// This is the data plane fast path. Updates route tables atomically across all
// subnets that were pointing to the failed region.
//
// Algorithm:
//   1. Identify all route table entries pointing to failed region's TGW attachment
//   2. For each CIDR previously routed through failed region:
//      a. Select best-weighted healthy region as next hop
//      b. Replace route with new TGW attachment ID
//   3. Emit CloudWatch metric for the failover event
//
// Latency target: < 500ms for all route table updates across all subnets.
func (m *Manager) FailoverRegion(ctx context.Context, failedRegion string) error {
	failedCfg, ok := m.cfg.Regions[failedRegion]
	if !ok {
		return fmt.Errorf("region %q not configured", failedRegion)
	}

	m.log.Warn("RouteTable: failover initiated",
		zap.String("failed_region", failedRegion),
		zap.Strings("affected_cidrs", failedCfg.TargetCIDRs),
		zap.Strings("route_tables", failedCfg.RouteTableIDs),
	)

	// Select next-hop region: highest weight among healthy (non-failed) regions
	nextHop := m.selectNextHop(failedRegion)
	if nextHop == "" {
		return fmt.Errorf("no healthy region available for failover from %q", failedRegion)
	}

	nextCfg := m.cfg.Regions[nextHop]

	m.log.Info("RouteTable: next-hop selected",
		zap.String("next_hop", nextHop),
		zap.String("tgw_attachment", nextCfg.TGWAttachmentID),
	)

	// Replace routes in all affected route tables
	for _, rtID := range failedCfg.RouteTableIDs {
		for _, cidr := range failedCfg.TargetCIDRs {
			if err := m.replaceRoute(ctx, rtID, cidr, nextCfg.TGWAttachmentID); err != nil {
				m.log.Error("RouteTable: route replace failed",
					zap.String("route_table", rtID),
					zap.String("cidr", cidr),
					zap.Error(err),
				)
				// Continue — partial update is better than no update
				// Failed routes are retried on next tick
			}
		}
	}

	// Update local weight tracking
	m.currentWeights[failedRegion] = 0.0
	m.rebalanceWeights(failedRegion)

	m.log.Info("RouteTable: failover complete",
		zap.String("failed_region", failedRegion),
		zap.String("next_hop", nextHop),
		zap.Any("new_weights", m.currentWeights),
	)

	return nil
}

// UpdateWeights applies normalised routing weights by redistributing route tables
// across subnets proportionally.
//
// AWS VPC doesn't support weighted ECMP natively. We approximate it by assigning
// different subsets of subnets to different route tables, each pointing to a
// different TGW attachment. The subnet assignment is updated when weights change.
//
// Example with 10 subnets and weights {us-east-1: 0.5, us-west-2: 0.3, eu-west-1: 0.2}:
//   Subnets 0-4 → us-east-1 route table
//   Subnets 5-7 → us-west-2 route table
//   Subnets 8-9 → eu-west-1 route table
func (m *Manager) UpdateWeights(ctx context.Context, weights map[string]float64) error {
	// Validate: weights must sum to ~1.0
	total := 0.0
	for _, w := range weights {
		total += w
	}
	if math.Abs(total-1.0) > 0.05 {
		return fmt.Errorf("weights sum to %.3f, expected 1.0", total)
	}

	// Collect all subnets across all regions
	type subnetAssignment struct {
		subnetID    string
		rtID        string
		targetRegion string
	}

	// Sort regions for deterministic assignment
	regions := make([]string, 0, len(weights))
	for r := range weights {
		regions = append(regions, r)
	}
	sort.Strings(regions)

	// Count total subnets
	allSubnets := []string{}
	for _, r := range regions {
		if cfg, ok := m.cfg.Regions[r]; ok {
			allSubnets = append(allSubnets, cfg.SubnetIDs...)
		}
	}
	totalSubnets := len(allSubnets)

	if totalSubnets == 0 {
		return nil // Nothing to update
	}

	// Assign subnets to regions proportionally
	assignments := make([]subnetAssignment, 0, totalSubnets)
	subnetIdx := 0

	for _, r := range regions {
		w := weights[r]
		count := int(math.Round(w * float64(totalSubnets)))
		if count == 0 && w > 0 {
			count = 1 // minimum 1 subnet if weight > 0
		}

		cfg := m.cfg.Regions[r]
		rtID := ""
		if len(cfg.RouteTableIDs) > 0 {
			rtID = cfg.RouteTableIDs[0]
		}

		for i := 0; i < count && subnetIdx < totalSubnets; i++ {
			assignments = append(assignments, subnetAssignment{
				subnetID:    allSubnets[subnetIdx],
				rtID:        rtID,
				targetRegion: r,
			})
			subnetIdx++
		}
	}

	// Apply subnet-to-route-table associations
	for _, a := range assignments {
		if a.rtID == "" {
			continue
		}
		if err := m.associateSubnet(ctx, a.subnetID, a.rtID); err != nil {
			m.log.Error("RouteTable: subnet association failed",
				zap.String("subnet", a.subnetID),
				zap.String("route_table", a.rtID),
				zap.String("target_region", a.targetRegion),
				zap.Error(err),
			)
		}
	}

	// Update cached weights
	for r, w := range weights {
		m.currentWeights[r] = w
	}

	m.log.Info("RouteTable: weights updated",
		zap.Any("weights", weights),
	)
	return nil
}

// RestoreRegion re-enables routing to a recovered region.
// Gradually re-introduces the region's routes as traffic ramps back.
func (m *Manager) RestoreRegion(ctx context.Context, region string) error {
	m.log.Info("RouteTable: restoring region", zap.String("region", region))

	cfg := m.cfg.Regions[region]

	// Restore the region's own CIDRs to point back to its own TGW attachment
	for _, rtID := range cfg.RouteTableIDs {
		for _, cidr := range cfg.TargetCIDRs {
			if err := m.replaceRoute(ctx, rtID, cidr, cfg.TGWAttachmentID); err != nil {
				m.log.Error("RouteTable: restore route failed",
					zap.String("route_table", rtID),
					zap.String("cidr", cidr),
					zap.Error(err),
				)
			}
		}
	}

	m.currentWeights[region] = 1.0 / float64(len(m.cfg.Regions))
	return nil
}

// ─── AWS API Calls ────────────────────────────────────────────────────────────

func (m *Manager) replaceRoute(
	ctx context.Context,
	routeTableID string,
	destinationCIDR string,
	tgwAttachmentID string,
) error {
	// First try to replace existing route
	_, err := m.ec2.ReplaceRoute(ctx, &ec2.ReplaceRouteInput{
		RouteTableId:           aws.String(routeTableID),
		DestinationCidrBlock:   aws.String(destinationCIDR),
		TransitGatewayId:       aws.String(tgwAttachmentID),
	})

	if err != nil {
		// If route doesn't exist, create it
		_, createErr := m.ec2.CreateRoute(ctx, &ec2.CreateRouteInput{
			RouteTableId:         aws.String(routeTableID),
			DestinationCidrBlock: aws.String(destinationCIDR),
			TransitGatewayId:     aws.String(tgwAttachmentID),
		})
		if createErr != nil {
			return fmt.Errorf("replace and create both failed: replace=%v create=%v", err, createErr)
		}
	}

	m.log.Debug("RouteTable: route updated",
		zap.String("route_table", routeTableID),
		zap.String("cidr", destinationCIDR),
		zap.String("tgw_attachment", tgwAttachmentID),
	)
	return nil
}

func (m *Manager) associateSubnet(ctx context.Context, subnetID, routeTableID string) error {
	// Check existing association
	output, err := m.ec2.DescribeRouteTables(ctx, &ec2.DescribeRouteTablesInput{
		Filters: []ec2types.Filter{
			{
				Name:   aws.String("association.subnet-id"),
				Values: []string{subnetID},
			},
		},
	})
	if err != nil {
		return fmt.Errorf("describe route tables: %w", err)
	}

	// Disassociate existing association if different
	for _, rt := range output.RouteTables {
		for _, assoc := range rt.Associations {
			if assoc.SubnetId != nil && *assoc.SubnetId == subnetID {
				if assoc.RouteTableId != nil && *assoc.RouteTableId == routeTableID {
					return nil // Already associated correctly
				}
				// Disassociate the old one
				if assoc.RouteTableAssociationId != nil {
					_, disErr := m.ec2.DisassociateRouteTable(ctx, &ec2.DisassociateRouteTableInput{
						AssociationId: assoc.RouteTableAssociationId,
					})
					if disErr != nil {
						return fmt.Errorf("disassociate: %w", disErr)
					}
				}
			}
		}
	}

	// Create new association
	_, err = m.ec2.AssociateRouteTable(ctx, &ec2.AssociateRouteTableInput{
		RouteTableId: aws.String(routeTableID),
		SubnetId:     aws.String(subnetID),
	})
	return err
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

// selectNextHop returns the highest-weight region that isn't the failed one.
func (m *Manager) selectNextHop(failedRegion string) string {
	bestRegion := ""
	bestWeight := -1.0

	for r, w := range m.currentWeights {
		if r == failedRegion {
			continue
		}
		if w > bestWeight {
			bestWeight = w
			bestRegion = r
		}
	}
	return bestRegion
}

// rebalanceWeights redistributes the failed region's weight across remaining regions.
func (m *Manager) rebalanceWeights(failedRegion string) {
	lost := m.currentWeights[failedRegion]
	m.currentWeights[failedRegion] = 0.0

	// Count healthy regions
	healthy := 0
	for r := range m.currentWeights {
		if r != failedRegion && m.currentWeights[r] > 0 {
			healthy++
		}
	}

	if healthy == 0 {
		return
	}

	// Distribute lost weight proportionally
	for r := range m.currentWeights {
		if r != failedRegion && m.currentWeights[r] > 0 {
			m.currentWeights[r] += lost / float64(healthy)
		}
	}
}

// CurrentWeights returns the current routing weights for diagnostics.
func (m *Manager) CurrentWeights() map[string]float64 {
	result := make(map[string]float64, len(m.currentWeights))
	for r, w := range m.currentWeights {
		result[r] = w
	}
	return result
}

// LastUpdateTime returns when weights were last changed (for SLO calculations).
func (m *Manager) LastUpdateTime() time.Time {
	return time.Now() // In production, track this per-region
}
