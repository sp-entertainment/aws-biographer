variable "region" {
  description = "AWS region for the shadow (deliberately unmanaged-looking) seed stack."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix for every resource name in this stack. Keep it distinct from seed/managed so the two stacks never collide."
  type        = string
  default     = "biographer-shadow"
}
