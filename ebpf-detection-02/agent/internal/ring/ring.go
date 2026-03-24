// Package ring implements a consistent hash ring with virtual nodes.
//
// This is the Dynamo-style ownership layer that makes split-brain impossible
// by construction — flow ownership is deterministic. When a peer fails,
// only the virtual nodes that rehash are affected; everything else is stable.
//
// Design choices:
//   - 150 virtual nodes per peer (from the NimbusNet architecture spec)
//   - FNV-1a hash (fast, good distribution, no crypto overhead)
//   - Thread-safe (RWMutex); reads are concurrent, writes lock briefly

package ring

import (
	"fmt"
	"hash/fnv"
	"sort"
	"sync"
)

const defaultVirtualNodes = 150

// Ring is a consistent hash ring with virtual nodes.
type Ring struct {
	mu           sync.RWMutex
	virtualNodes int
	sortedHashes []uint32
	hashToNode   map[uint32]string // hash → peer node ID
}

// New creates a Ring with the given virtual node count and initial peer list.
func New(virtualNodes int, peers []string) *Ring {
	if virtualNodes <= 0 {
		virtualNodes = defaultVirtualNodes
	}
	r := &Ring{
		virtualNodes: virtualNodes,
		hashToNode:   make(map[uint32]string),
	}
	for _, p := range peers {
		r.addPeer(p)
	}
	r.rebuild()
	return r
}

// AddPeer adds a new node to the ring. Safe to call at runtime (node join).
func (r *Ring) AddPeer(nodeID string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.addPeer(nodeID)
	r.rebuild()
}

// RemovePeer removes a node from the ring. Safe to call at runtime (node failure).
// Only the virtual nodes belonging to this peer rehash — everything else is stable.
func (r *Ring) RemovePeer(nodeID string) {
	r.mu.Lock()
	defer r.mu.Unlock()

	// Remove all virtual nodes for this peer
	for h, n := range r.hashToNode {
		if n == nodeID {
			delete(r.hashToNode, h)
		}
	}
	r.rebuild()
}

// Owner returns the node ID that owns the given key.
// Deterministic: same key always returns same owner unless the ring changes.
func (r *Ring) Owner(key string) string {
	r.mu.RLock()
	defer r.mu.RUnlock()

	if len(r.sortedHashes) == 0 {
		return ""
	}

	h := hash(key)
	// Binary search for the first virtual node >= h
	idx := sort.Search(len(r.sortedHashes), func(i int) bool {
		return r.sortedHashes[i] >= h
	})

	// Wrap around — ring is circular
	if idx >= len(r.sortedHashes) {
		idx = 0
	}

	return r.hashToNode[r.sortedHashes[idx]]
}

// Peers returns the list of unique node IDs currently in the ring.
func (r *Ring) Peers() []string {
	r.mu.RLock()
	defer r.mu.RUnlock()

	seen := make(map[string]struct{})
	for _, n := range r.hashToNode {
		seen[n] = struct{}{}
	}
	peers := make([]string, 0, len(seen))
	for n := range seen {
		peers = append(peers, n)
	}
	sort.Strings(peers)
	return peers
}

// VirtualNodeCount returns the number of virtual nodes currently in the ring.
func (r *Ring) VirtualNodeCount() int {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return len(r.sortedHashes)
}

// ─── Internal ─────────────────────────────────────────────────────────────────

func (r *Ring) addPeer(nodeID string) {
	for i := 0; i < r.virtualNodes; i++ {
		vkey := fmt.Sprintf("%s#%d", nodeID, i)
		h := hash(vkey)
		r.hashToNode[h] = nodeID
	}
}

func (r *Ring) rebuild() {
	r.sortedHashes = make([]uint32, 0, len(r.hashToNode))
	for h := range r.hashToNode {
		r.sortedHashes = append(r.sortedHashes, h)
	}
	sort.Slice(r.sortedHashes, func(i, j int) bool {
		return r.sortedHashes[i] < r.sortedHashes[j]
	})
}

func hash(key string) uint32 {
	h := fnv.New32a()
	h.Write([]byte(key))
	return h.Sum32()
}
