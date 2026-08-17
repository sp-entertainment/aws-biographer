output "region" {
  description = "Region this stack was applied to."
  value       = var.region
}

output "eip_allocation_id" {
  description = "Allocation ID of the deliberately unattached Elastic IP."
  value       = aws_eip.orphaned.id
}

output "eip_public_ip" {
  description = "Public IPv4 address of the unattached Elastic IP."
  value       = aws_eip.orphaned.public_ip
}

output "ebs_volume_ids" {
  description = "IDs of the two unattached gp3 volumes (tagged, untagged)."
  value = {
    tagged   = aws_ebs_volume.orphaned_tagged.id
    untagged = aws_ebs_volume.orphaned_untagged.id
  }
}

output "s3_bucket_names" {
  description = "The three inconsistently named buckets, keyed by naming style."
  value       = { for k, b in aws_s3_bucket.sloppy : k => b.id }
}

output "s3_bucket_arns" {
  description = "ARNs of the three buckets."
  value       = { for k, b in aws_s3_bucket.sloppy : k => b.arn }
}

output "security_group_id" {
  description = "ID of the deliberately world-open RDP security group."
  value       = aws_security_group.rdp_open_to_world.id
}

output "lambda_function_name" {
  description = "Name of the never-invoked Lambda function."
  value       = aws_lambda_function.never_invoked.function_name
}

output "lambda_function_arn" {
  description = "ARN of the never-invoked Lambda function."
  value       = aws_lambda_function.never_invoked.arn
}

output "lambda_role_arn" {
  description = "ARN of the Lambda execution role."
  value       = aws_iam_role.never_invoked.arn
}

output "sqs_queue_url" {
  description = "URL of the orphaned SQS queue."
  value       = aws_sqs_queue.unwired.url
}

output "sqs_queue_arn" {
  description = "ARN of the orphaned SQS queue."
  value       = aws_sqs_queue.unwired.arn
}

output "sns_topic_arn" {
  description = "ARN of the orphaned SNS topic."
  value       = aws_sns_topic.unwired.arn
}

output "dynamodb_table_name" {
  description = "Name of the empty on-demand DynamoDB table."
  value       = aws_dynamodb_table.unused.name
}

output "dynamodb_table_arn" {
  description = "ARN of the empty on-demand DynamoDB table."
  value       = aws_dynamodb_table.unused.arn
}
