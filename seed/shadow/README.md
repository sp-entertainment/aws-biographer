# seed/shadow

Terraform root module that seeds the contest demo account (
us-east-1) with resources that are meant to look **unmanaged**.

## Why this stack is separate — read before merging it into `seed/managed/`

The drift feature answers "how much of this account is covered by IaC?". To
answer that it reads the Terraform state of `seed/managed/` and compares it
against what is actually live in the account. If every live resource appears in
that state, coverage is 100%, the drift number is always zero, and the feature
demos as a blank screen.

This stack exists to be the other side of that ratio. Its resources are live in
the account but absent from the state the analyzer reads, so they are reported
as untracked. They are still Terraform-created, so `terraform destroy` removes
them cleanly and nothing is orphaned in the account.

Three consequences, all load-bearing:

1. **This stack's state file must NEVER be handed to the drift analyzer.**
   Not as a second `-state` argument, not merged into a workspace it reads, not
   pointed at by a shared backend key. The moment the analyzer can see this
   state, every finding below becomes "managed" and the drift signal drops to
   zero.
2. **Do not merge these files into `seed/managed/`.** Same reason. If you are
   here because "there are two Terraform stacks and that looks wrong" — it is
   deliberate, and this paragraph is the answer.
3. **Do not "fix" the misconfigurations.** The unattached EIP, the unattached
   volumes, the world-open RDP rule, the untagged resources, and the sloppy
   bucket names are the fixtures the scanner is supposed to find. Every one of
   them carries an `INTENTIONAL` comment in `main.tf`.

This stack deliberately sets NO default tags -- several resources exist
precisely to be untagged, and a blanket tag would erase that finding.
Cleanup therefore relies on Terraform state, which covers every resource here.
Terraform state, which covers every resource this stack creates.

## What it creates

| Resource | Intended finding |
| --- | --- |
| 1 Elastic IP, attached to nothing | Unattached EIP (the flagship cost finding) |
| 2 gp3 EBS volumes, 1 GiB, attached to nothing (one tagged, one not) | Unattached EBS volumes; missing tags |
| 3 S3 buckets with inconsistent names | No naming standard; untagged resources |
| 1 security group, 3389 open to 0.0.0.0/0 | Admin port open to the world |
| 1 Lambda function with no trigger | Idle / never-invoked function |
| 1 SQS queue + 1 SNS topic, unwired | Orphaned messaging infrastructure |
| 1 DynamoDB table (on-demand), empty | Unused table |

No NAT gateway, no RDS, no load balancer, no EC2 instance, no ECS/EKS, no
interface VPC endpoints, no provisioned-capacity DynamoDB. Nothing here has an
hourly floor except the Elastic IP.

## Estimated cost

| Item | Monthly |
| --- | --- |
| Unattached Elastic IP (`$0.005/hr`) | ~$3.60 |
| 2 x 1 GiB gp3 (`$0.08/GiB-month`) | ~$0.16 |
| S3 (empty), SQS, SNS, IAM, security group | $0.00 |
| Lambda (never invoked; code storage well under the free tier) | $0.00 |
| DynamoDB on-demand, empty table | ~$0.00 |
| **Total** | **~$3.80** |

## Applying in tranches

Apply in separate steps a few minutes apart so CloudTrail records the creations
spread over time instead of one flat burst — a real account does not materialize
in a single second, and the demo timeline reads better with gaps.

```sh
terraform init

# Tranche 1 — compute-adjacent waste
terraform apply \
  -target=aws_eip.orphaned \
  -target=aws_ebs_volume.orphaned_tagged \
  -target=aws_ebs_volume.orphaned_untagged

# ... wait a few minutes ...

# Tranche 2 — storage
terraform apply \
  -target=aws_s3_bucket.sloppy \
  -target=aws_s3_bucket_public_access_block.sloppy

# ... wait a few minutes ...

# Tranche 3 — network
terraform apply -target=aws_security_group.rdp_open_to_world

# ... wait a few minutes ...

# Tranche 4 — compute + IAM
terraform apply \
  -target=aws_iam_role.never_invoked \
  -target=aws_iam_role_policy_attachment.never_invoked_basic \
  -target=aws_lambda_function.never_invoked

# ... wait a few minutes ...

# Tranche 5 — messaging and data
terraform apply \
  -target=aws_sqs_queue.unwired \
  -target=aws_sns_topic.unwired \
  -target=aws_dynamodb_table.unused

# Final pass with no -target: reconciles anything the targeted runs skipped
# and produces the full output set.
terraform apply
```

`-target` runs emit a "resource targeting is in effect" warning. That is
expected here; the final untargeted `apply` clears it.

## Destroying

```sh
terraform destroy
```

S3 buckets must be empty to delete. Nothing writes to them, but if you put
objects there by hand, empty them first:

```sh
aws s3 rm "s3://$(terraform output -json s3_bucket_names | jq -r .dated)" --recursive
```

Belt-and-braces sweep if the state is ever lost — every resource carries
Terraform state:

```sh
aws resourcegroupstaggingapi get-resources \
  --region us-east-1
```

## State

Local `terraform.tfstate` by default, and that is fine — it keeps this state
physically apart from whatever backend `seed/managed/` uses. If you move it to a
remote backend, give it a key that no drift-analyzer configuration can resolve,
and re-read the "Why this stack is separate" section above first.
