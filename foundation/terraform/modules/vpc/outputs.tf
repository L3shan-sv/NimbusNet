output "vpc_id" {
  description = "VPC ID"
  value       = aws_vpc.main.id
}

output "vpc_cidr" {
  description = "VPC CIDR block"
  value       = aws_vpc.main.cidr_block
}

output "public_subnet_ids" {
  description = "IDs of public subnets"
  value       = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  description = "IDs of private subnets"
  value       = aws_subnet.private[*].id
}

output "intra_subnet_ids" {
  description = "IDs of intra subnets (TGW attachments)"
  value       = aws_subnet.intra[*].id
}

output "private_route_table_ids" {
  description = "IDs of private route tables (used for TGW route injection)"
  value       = aws_route_table.private[*].id
}

output "intra_route_table_id" {
  description = "ID of intra route table"
  value       = aws_route_table.intra.id
}
