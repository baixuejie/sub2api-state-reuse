package main

import (
	"encoding/base64"
	"encoding/binary"
	"testing"
	"time"
)

func TestAcceptedTicketLengths(t *testing.T) {
	for _, n := range []int{217, 249, 233, 265, 218} {
		b := make([]byte, n)
		b[0] = 128
		binary.BigEndian.PutUint64(b[1:9], uint64(time.Now().Unix()))
		s := base64.URLEncoding.EncodeToString(b)
		_, ok := parseState(s)
		if ok != (n == 217 || n == 249) {
			t.Fatalf("decoded=%d encoded=%d accepted=%t", n, len(s), ok)
		}
	}
}
