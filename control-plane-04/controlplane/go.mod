module github.com/nimbusnet/controlplane

go 1.22

require (
	github.com/aws/aws-sdk-go-v2 v1.26.0
	github.com/aws/aws-sdk-go-v2/config v1.27.0
	github.com/aws/aws-sdk-go-v2/service/route53 v1.40.0
	github.com/aws/aws-sdk-go-v2/service/ec2 v1.151.0
	github.com/aws/aws-sdk-go-v2/service/dynamodb v1.31.0
	github.com/aws/aws-sdk-go-v2/service/cloudwatch v1.38.0
	github.com/prometheus/client_golang v1.19.0
	go.uber.org/zap v1.27.0
	gopkg.in/yaml.v3 v3.0.1
)
