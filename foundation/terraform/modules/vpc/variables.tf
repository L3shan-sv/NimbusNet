variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
}

variable "region_name" {
  description = "Short region identifier used in resource names (e.g. us-east-1)"
  type        = string
}

variable "availability_zones" {
  description = "List of availability zones for subnet placement"
  type        = list(string)
}

variable "public_subnet_cidrs" {
  description = "CIDR blocks for public subnets (ALB only)"
  type        = list(string)
}

variable "private_subnet_cidrs" {
  description = "CIDR blocks for private subnets (application workloads)"
  type        = list(string)
}

variable "intra_subnet_cidrs" {
  description = "CIDR blocks for intra subnets (TGW attachments — no internet route)"
  type        = list(string)
}

variable "enable_nat_gateway" {
  description = "Whether to create NAT gateways. False for cold standby regions."
  type        = bool
  default     = true
}

variable "flow_log_iam_role_arn" {
  description = "IAM role ARN for VPC flow log delivery to CloudWatch"
  type        = string
}

variable "tags" {
  description = "Common tags applied to all resources"
  type        = map(string)
  default     = {}
}
