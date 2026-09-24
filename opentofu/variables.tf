# opentofu/variables.tf
variable "region" { type = string }
variable "bucket_name" {
  type        = string
  description = "Globally-unique S3 bucket name for the off-site backups."
}
variable "name_prefix" {
  type    = string
  default = "backup-engine"
}
variable "base_bucket_versioned" {
  type        = bool
  default     = true
  description = "Versioning status of the base backup bucket. Suspend only for a deliberate migration; per-job dedicated buckets are unaffected."
}
