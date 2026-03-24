variable "region_name"       { type = string }
variable "vpc_id"             { type = string }
variable "public_subnet_ids"  { type = list(string) }
variable "security_group_id"  { type = string }
variable "access_logs_bucket" { type = string; default = "" }
variable "tags"               { type = map(string); default = {} }
