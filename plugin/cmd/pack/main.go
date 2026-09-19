package main

import (
	"archive/zip"
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"os"
)

func main() {
	source, err := os.ReadFile("main.go")
	if err != nil || !bytes.Contains(source, []byte(`const version = "1.0.12"`)) {
		panic("runtime version must match package version 1.0.12")
	}
	key, e := os.ReadFile("publisher.key")
	var priv ed25519.PrivateKey
	if os.IsNotExist(e) {
		_, priv, e = ed25519.GenerateKey(rand.Reader)
		if e != nil {
			panic(e)
		}
		if e = os.WriteFile("publisher.key", priv, 0600); e != nil {
			panic(e)
		}
	} else {
		if e != nil {
			panic(e)
		}
		if len(key) != ed25519.PrivateKeySize {
			panic("invalid signing key")
		}
		priv = ed25519.PrivateKey(key)
	}
	pub := priv.Public().(ed25519.PublicKey)
	bin, e := os.ReadFile("state-reuse")
	if e != nil {
		panic(e)
	}
	files := map[string][]byte{"runtimes/linux-amd64/state-reuse": bin, "ui/index.html": []byte(`<!doctype html><meta charset="utf-8"><title>STATE Reuse</title><h1>STATE 292/332 Reuse</h1><p>Account, credential and model isolated. Missing tickets forward normally; Astra tickets determine grouping. Configuration managed by server runbook.</p>`)}
	hashes := map[string]string{}
	for k, v := range files {
		h := sha256.Sum256(v)
		hashes[k] = hex.EncodeToString(h[:])
	}
	manifest := map[string]any{"schema_version": 2, "id": "local.flownode.state-reuse", "name": "FlowNode STATE 292/332 Reuse", "version": "1.0.12", "description": "Persistent account/model/credential scoped STATE reuse and guarded renewal", "requires": map[string]any{"sub2api": ">=0.2.5 <0.3.0", "plugin_protocol": 2, "extension_api": 1, "ui_bridge": 1}, "capabilities": []any{map[string]any{"id": "openai.oauth.protection_transport.v1", "kind": "provider", "platform": "openai", "account_type": "oauth", "permissions": []string{"request.metadata.read", "request.body.read", "request.credentials.forward", "network.outbound", "account.protection.read", "request.original.read"}, "timeout_ms": 120000, "failure_mode": "fail_closed", "synchronous": true}}, "runtimes": map[string]any{"linux-amd64": map[string]string{"path": "runtimes/linux-amd64/state-reuse"}}, "ui": map[string]string{"entrypoint": "ui/index.html"}, "files": hashes}
	m, _ := json.Marshal(manifest)
	sig, _ := json.Marshal(map[string]string{"algorithm": "ed25519", "key_id": "local-flownode-state-reuse-20260919", "public_key": base64.StdEncoding.EncodeToString(pub), "signature": base64.StdEncoding.EncodeToString(ed25519.Sign(priv, m))})
	files["manifest.json"] = m
	files["signature.json"] = sig
	f, e := os.Create("state-reuse-1.0.12.s2plugin")
	if e != nil {
		panic(e)
	}
	z := zip.NewWriter(f)
	for name, b := range files {
		w, e := z.Create(name)
		if e != nil {
			panic(e)
		}
		w.Write(b)
	}
	z.Close()
	f.Close()
}
