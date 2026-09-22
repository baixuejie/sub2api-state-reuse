package main

import (
	"context"

	v1 "github.com/Wei-Shaw/sub2api/pkg/pluginapi/v1"
	"google.golang.org/grpc"
)

const compatibilityVersion = version + "+sub2api.v1.5"
const v1Capability = "openai.oauth.outbound_transport.v1"

// v1Adapter keeps the plugin's ticket implementation independent of the
// original Sub2API host's transport and lifecycle RPC contract.
type v1Adapter struct {
	v1.UnimplementedTransportPluginServer
	plugin *Plugin
}

func (a *v1Adapter) GetInfo(context.Context, *v1.GetInfoRequest) (*v1.GetInfoResponse, error) {
	return &v1.GetInfoResponse{PluginId: pluginID, PluginVersion: compatibilityVersion,
		ProtocolVersion: 1, TransportApiVersion: 1, Capabilities: []string{v1Capability}}, nil
}

func (a *v1Adapter) Health(ctx context.Context, _ *v1.HealthRequest) (*v1.HealthResponse, error) {
	health, err := a.plugin.Health(ctx)
	if err != nil {
		return nil, err
	}
	return &v1.HealthResponse{Healthy: health.Healthy, Message: health.Message}, nil
}

func (a *v1Adapter) ValidateConfig(ctx context.Context, request *v1.ValidateConfigRequest) (*v1.ValidateConfigResponse, error) {
	normalized, err := a.plugin.ValidateConfig(ctx, request.GetConfigJson())
	if err != nil {
		return &v1.ValidateConfigResponse{Message: err.Error()}, nil
	}
	return &v1.ValidateConfigResponse{Valid: true, NormalizedConfigJson: normalized}, nil
}

func (a *v1Adapter) ApplyConfig(ctx context.Context, request *v1.ApplyConfigRequest) (*v1.ApplyConfigResponse, error) {
	if err := a.plugin.ApplyConfig(ctx, request.GetConfigJson()); err != nil {
		return &v1.ApplyConfigResponse{Message: err.Error()}, nil
	}
	return &v1.ApplyConfigResponse{Applied: true}, nil
}

func (a *v1Adapter) TestConfig(ctx context.Context, request *v1.TestConfigRequest) (*v1.TestConfigResponse, error) {
	duration, err := a.plugin.TestConfig(ctx, request.GetConfigJson())
	if err != nil {
		return &v1.TestConfigResponse{Message: err.Error()}, nil
	}
	return &v1.TestConfigResponse{Success: true, Message: "Configuration valid", LatencyMs: duration.Milliseconds()}, nil
}

func (a *v1Adapter) Forward(stream grpc.BidiStreamingServer[v1.ForwardRequest, v1.ForwardResponse]) error {
	return a.plugin.Forward(stream)
}
