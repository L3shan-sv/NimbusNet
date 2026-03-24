variable "vpc_id"           { type = string }
variable "region_name"      { type = string }
variable "vpc_cidr"         { type = string }
variable "remote_vpc_cidrs" { type = list(string); default = [] }
variable "tags"             { type = map(string); default = {} }
