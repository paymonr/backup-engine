# opentofu/variables.tf
variable "region" { type = string }
variable "bucket_name" {
  type        = string
  description = "Globally-unique S3 bucket name for the off-site backups."
}
variable "name_prefix" {
  type    = string
  default = "unraid-s3-backup"
}
variable "noncurrent_version_expiration_days" {
  type    = number
  default = 30
}
variable "abort_incomplete_multipart_days" {
  type    = number
  default = 7
}
