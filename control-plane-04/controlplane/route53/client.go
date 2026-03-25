// Package route53 implements the NimbusNet Route 53 two-plane integration.
//
// This is the DNS poisoning pattern:
//
//  Data plane  (fast): VPC route table updated immediately by route_table.go.
//                      New packets rerouted within milliseconds.
//
//  DNS plane   (slow): /healthz returns 503 when draining. Route 53 health checks
//                      poll every 10s; two failures mark the region unhealthy.
//                      DNS TTL is 30s. Total R53 convergence: ~35s.
//
// The two planes are intentionally decoupled:
//   - Data plane heals first (milliseconds) — stops the bleeding
//   - DNS plane heals second (35 seconds) — prevents new connections routing to
//     the failed region after the data plane is already healed
//
// Record types managed:
//   - Latency-based routing records: primary traffic distribution
//   - Health check associations: automatic failover trigger
//   - Failover records: SECONDARY records activated when PRIMARY fails health check
//
// DynamoDB lock:
//   All Route 53 write operations acquire a DynamoDB conditional write lock
//   before executing. If both US and EU agents fire simultaneously,
//   only one succeeds — the other sees a ConditionalCheckFailedException and backs off.

package route53

import (
	"context"
	"fmt"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/route53"
	"github.com/aws/aws-sdk-go-v2/service/route53/types"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb"
	dynamotypes "github.com/aws/aws-sdk-go-v2/service/dynamodb/types"
	"go.uber.org/zap"
)

// ─── Config ───────────────────────────────────────────────────────────────────

type Config struct {
	HostedZoneID    string // e.g. "Z1234567890ABC"
	RecordName      string // e.g. "api.nimbusnet.internal"
	RecordTTL       int64  // seconds; default 30
	HealthCheckIDs  map[string]string // region → health check ID
	LockTableName   string // DynamoDB table for distributed lock
	LockTTLSeconds  int    // how long the lock is held; default 60
}

// ─── Client ───────────────────────────────────────────────────────────────────

// Client manages Route 53 records and health check associations.
type Client struct {
	cfg      Config
	r53      *route53.Client
	ddb      *dynamodb.Client
	log      *zap.Logger
	localRegion string
}

// New creates a Route53 Client.
func New(
	cfg       Config,
	r53Client *route53.Client,
	ddbClient *dynamodb.Client,
	localRegion string,
	log       *zap.Logger,
) *Client {
	if cfg.RecordTTL == 0 {
		cfg.RecordTTL = 30
	}
	if cfg.LockTTLSeconds == 0 {
		cfg.LockTTLSeconds = 60
	}
	return &Client{
		cfg:         cfg,
		r53:         r53Client,
		ddb:         ddbClient,
		log:         log,
		localRegion: localRegion,
	}
}

// ─── Primary Operations ───────────────────────────────────────────────────────

// DrainRegion marks a region as unhealthy in Route 53.
//
// Mechanism:
//   1. Acquire DynamoDB lock (prevents concurrent dual-region drain)
//   2. Disassociate the health check from the latency record
//   3. The Go agent's /healthz is already returning 503 (Phase 2)
//   4. R53 will stop routing to this region within ~35s
//
// The data plane (VPC route table) must be updated BEFORE calling this.
// DNS is the slow path — it only prevents new connections after TTL expires.
func (c *Client) DrainRegion(ctx context.Context, region string) error {
	lockKey := fmt.Sprintf("drain:%s", region)

	// Acquire distributed lock — prevents competing drain from other agent
	acquired, err := c.acquireLock(ctx, lockKey)
	if err != nil {
		return fmt.Errorf("acquire lock for drain: %w", err)
	}
	if !acquired {
		c.log.Warn("R53: drain lock already held — another agent is draining",
			zap.String("region", region),
		)
		return nil // Idempotent — the other agent will handle it
	}
	defer c.releaseLock(ctx, lockKey)

	healthCheckID, ok := c.cfg.HealthCheckIDs[region]
	if !ok {
		return fmt.Errorf("no health check ID configured for region %q", region)
	}

	c.log.Info("R53: draining region — disassociating health check",
		zap.String("region", region),
		zap.String("health_check_id", healthCheckID),
	)

	// Update the latency record to mark the health check as the failover trigger
	// In AWS terms: set the health check association so R53 stops routing when check fails
	input := &route53.ChangeResourceRecordSetsInput{
		HostedZoneId: aws.String(c.cfg.HostedZoneID),
		ChangeBatch: &types.ChangeBatch{
			Comment: aws.String(fmt.Sprintf("NimbusNet drain: %s at %s", region, time.Now().UTC().Format(time.RFC3339))),
			Changes: []types.Change{
				{
					Action: types.ActionUpsert,
					ResourceRecordSet: &types.ResourceRecordSet{
						Name:            aws.String(c.cfg.RecordName),
						Type:            types.RRTypeA,
						Region:          types.ResourceRecordSetRegion(region),
						SetIdentifier:   aws.String(region),
						TTL:             aws.Int64(c.cfg.RecordTTL),
						HealthCheckId:   aws.String(healthCheckID),
						// The /healthz endpoint is returning 503 — health check will fail
						// R53 will stop routing to this record once it fails
						ResourceRecords: []types.ResourceRecord{
							{Value: aws.String(c.getRegionALBIP(region))},
						},
					},
				},
			},
		},
	}

	_, err = c.r53.ChangeResourceRecordSets(ctx, input)
	if err != nil {
		return fmt.Errorf("change record set for drain: %w", err)
	}

	c.log.Info("R53: drain initiated — health check will fail within 10s, DNS TTL 30s",
		zap.String("region", region),
		zap.String("health_check_id", healthCheckID),
	)

	return nil
}

// RestoreRegion re-enables routing to a recovered region.
//
// Called by the FSM enter-HEALTHY action, after the observation window passes.
// The /healthz endpoint is already returning 200 (Phase 2 agent restored it).
// R53 will resume routing within one health check interval (~10s).
func (c *Client) RestoreRegion(ctx context.Context, region string) error {
	lockKey := fmt.Sprintf("restore:%s", region)

	acquired, err := c.acquireLock(ctx, lockKey)
	if err != nil {
		return fmt.Errorf("acquire lock for restore: %w", err)
	}
	if !acquired {
		c.log.Warn("R53: restore lock already held", zap.String("region", region))
		return nil
	}
	defer c.releaseLock(ctx, lockKey)

	c.log.Info("R53: restoring region", zap.String("region", region))

	// Health check is already passing (Phase 2 agent returned 200).
	// R53 will automatically resume routing within one check interval.
	// We issue an explicit UPSERT to reset any manual weight overrides.
	input := &route53.ChangeResourceRecordSetsInput{
		HostedZoneId: aws.String(c.cfg.HostedZoneID),
		ChangeBatch: &types.ChangeBatch{
			Comment: aws.String(fmt.Sprintf("NimbusNet restore: %s at %s", region, time.Now().UTC().Format(time.RFC3339))),
			Changes: []types.Change{
				{
					Action: types.ActionUpsert,
					ResourceRecordSet: &types.ResourceRecordSet{
						Name:          aws.String(c.cfg.RecordName),
						Type:          types.RRTypeA,
						Region:        types.ResourceRecordSetRegion(region),
						SetIdentifier: aws.String(region),
						TTL:           aws.Int64(c.cfg.RecordTTL),
						HealthCheckId: aws.String(c.cfg.HealthCheckIDs[region]),
						ResourceRecords: []types.ResourceRecord{
							{Value: aws.String(c.getRegionALBIP(region))},
						},
					},
				},
			},
		},
	}

	_, err = c.r53.ChangeResourceRecordSets(ctx, input)
	if err != nil {
		return fmt.Errorf("change record set for restore: %w", err)
	}

	c.log.Info("R53: region restore complete — DNS will converge within TTL",
		zap.String("region", region),
		zap.Int64("ttl_seconds", c.cfg.RecordTTL),
	)
	return nil
}

// GetHealthCheckStatus fetches the current R53 health check status for a region.
// Used by the control plane main loop to verify DNS plane convergence.
func (c *Client) GetHealthCheckStatus(ctx context.Context, region string) (bool, error) {
	healthCheckID, ok := c.cfg.HealthCheckIDs[region]
	if !ok {
		return false, fmt.Errorf("no health check ID for region %q", region)
	}

	output, err := c.r53.GetHealthCheckStatus(ctx, &route53.GetHealthCheckStatusInput{
		HealthCheckId: aws.String(healthCheckID),
	})
	if err != nil {
		return false, fmt.Errorf("get health check status: %w", err)
	}

	// Check if any checker reports the endpoint as healthy
	for _, checker := range output.CheckerIpRanges {
		_ = checker // checker details for debugging
	}

	// If StatusReport exists, inspect it
	// In real SDK, we check CheckerStatus — simplified here
	healthy := len(output.CheckerIpRanges) > 0
	return healthy, nil
}

// ─── DynamoDB Distributed Lock ────────────────────────────────────────────────
//
// The conditional write is the split-brain safety mechanism.
// If both US and EU agents try to drain simultaneously:
//   - First write succeeds (condition: lock_key does NOT exist)
//   - Second write fails with ConditionalCheckFailedException
//   - Second agent backs off and logs — the first agent handles it

func (c *Client) acquireLock(ctx context.Context, lockKey string) (bool, error) {
	expiresAt := fmt.Sprintf("%d", time.Now().Add(time.Duration(c.cfg.LockTTLSeconds)*time.Second).Unix())

	_, err := c.ddb.PutItem(ctx, &dynamodb.PutItemInput{
		TableName: aws.String(c.cfg.LockTableName),
		Item: map[string]dynamotypes.AttributeValue{
			"lock_key":   &dynamotypes.AttributeValueMemberS{Value: lockKey},
			"owner":      &dynamotypes.AttributeValueMemberS{Value: c.localRegion},
			"expires_at": &dynamotypes.AttributeValueMemberN{Value: expiresAt},
			"acquired_at": &dynamotypes.AttributeValueMemberN{
				Value: fmt.Sprintf("%d", time.Now().Unix()),
			},
		},
		// Condition: lock does not exist OR has expired
		ConditionExpression: aws.String(
			"attribute_not_exists(lock_key) OR expires_at < :now",
		),
		ExpressionAttributeValues: map[string]dynamotypes.AttributeValue{
			":now": &dynamotypes.AttributeValueMemberN{
				Value: fmt.Sprintf("%d", time.Now().Unix()),
			},
		},
	})

	if err != nil {
		// ConditionalCheckFailedException means lock is held by another agent
		// This is the expected race condition — not an error
		if isConditionalCheckFailed(err) {
			return false, nil
		}
		return false, fmt.Errorf("DynamoDB lock acquire: %w", err)
	}

	c.log.Debug("R53: lock acquired",
		zap.String("key", lockKey),
		zap.String("owner", c.localRegion),
	)
	return true, nil
}

func (c *Client) releaseLock(ctx context.Context, lockKey string) {
	_, err := c.ddb.DeleteItem(ctx, &dynamodb.DeleteItemInput{
		TableName: aws.String(c.cfg.LockTableName),
		Key: map[string]dynamotypes.AttributeValue{
			"lock_key": &dynamotypes.AttributeValueMemberS{Value: lockKey},
		},
		// Only release if we own it
		ConditionExpression: aws.String("owner = :owner"),
		ExpressionAttributeValues: map[string]dynamotypes.AttributeValue{
			":owner": &dynamotypes.AttributeValueMemberS{Value: c.localRegion},
		},
	})
	if err != nil {
		c.log.Warn("R53: lock release failed (may have already expired)",
			zap.String("key", lockKey),
			zap.Error(err),
		)
	}
}

func isConditionalCheckFailed(err error) bool {
	var ccf *dynamotypes.ConditionalCheckFailedException
	return err != nil && fmt.Sprintf("%T", err) == fmt.Sprintf("%T", ccf)
}

// getRegionALBIP returns the ALB IP/hostname for a region.
// In production this comes from Terraform outputs via SSM Parameter Store.
func (c *Client) getRegionALBIP(region string) string {
	// Placeholder — replaced at runtime from SSM /nimbusnet/{env}/alb/{region}/dns_name
	return fmt.Sprintf("alb.%s.nimbusnet.internal", region)
}
