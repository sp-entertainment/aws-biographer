variable "region" {
  description = "AWS region to seed."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix applied to resource names so seeded resources are easy to spot."
  type        = string
  default     = "biographer-seed"
}
