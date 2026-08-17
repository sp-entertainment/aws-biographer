# seed/managed - IaC-covered seed stack

Terraform root module that seeds the contest demo account (us-east-1) with
deliberately messy but cheap resources for the scanner demo.

This is the **IaC-covered half** of the seed. The drift analyzer reads this
module's state file and diffs it against live inventory, so everything
declared here must actually be managed here - do not create these resources
by hand and do not import unrelated ones.

## What it creates

| # | Resource | Deliberate finding |
|---|----------|--------------------|
| 1 | VPC `10.42.0.0/16`, public subnet, IGW, route table | subnet has **no Name tag** |
| 2 | Security group `<prefix>-web` | **SSH 22 open to 0.0.0.0/0** |
| 3 | One `t4g.nano` EC2 (AL2023 arm64, gp3 8GB) | **no tags at all** |
| 4 | Two S3 buckets | **inconsistent naming**; only one has versioning |
| 5 | Lambda `<prefix>-noop` (python3.13, 128MB) + IAM role | **never invoked**; its log group is **not** in Terraform |
| 6 | CloudWatch log group `/<prefix>/app` | **no retention policy** (never expires) |

Every one of those is marked with an `INTENTIONAL` comment in `main.tf`.
They are the demo payload - do not "fix" them.

## Cost

Roughly **$3.70/month**, effectively all of it the EC2 instance
(t4g.nano ~$3.07 + 8GB gp3 ~$0.64). S3, Lambda, IAM, logs and the VPC are
free or fractions of a cent at this scale.

Two deliberate cost guards, keep them:

- `associate_public_ip_address = false` / `map_public_ip_on_launch = false` -
  a public IPv4 address is billed hourly (~$3.65/mo) and would nearly double
  this stack.
- No NAT gateway, no load balancer, no RDS, no interface VPC endpoints. These
  have hourly floors that break the budget.

## Apply in tranches

Applying everything in one shot puts a single flat burst into CloudTrail.
Spreading it over three targeted applies makes the event timeline look like
real activity. Leave a gap of **10-20 minutes** between tranches (longer is
fine; CloudTrail delivers on ~5-15 minute latency anyway).

```sh
cd seed/managed
terraform init
```

### Tranche 1 - networking

```sh
terraform apply \
  -target=aws_vpc.seed \
  -target=aws_subnet.public \
  -target=aws_internet_gateway.seed \
  -target=aws_route_table.public \
  -target=aws_route_table_association.public \
  -target=aws_security_group.web
```

Wait ~15 minutes.

### Tranche 2 - storage and logging

```sh
terraform apply \
  -target=aws_s3_bucket.assets_prod \
  -target=aws_s3_bucket.backups_2024 \
  -target=aws_s3_bucket_versioning.assets_prod \
  -target=aws_cloudwatch_log_group.forever
```

Wait ~15 minutes.

### Tranche 3 - compute

```sh
terraform apply \
  -target=aws_instance.orphan \
  -target=aws_iam_role.lambda \
  -target=aws_iam_role_policy_attachment.lambda_basic \
  -target=aws_lambda_function.noop
```

### Final reconcile

`-target` applies leave Terraform warning that state is partial. Finish with a
plain apply to confirm the whole module converged:

```sh
terraform apply
```

It should report **no changes**. If it wants to create something, a tranche
missed a resource.

## Destroy

```sh
terraform destroy
```

That removes everything this module manages. Two things it will **not** clean
up, by design:

- `/aws/lambda/<prefix>-noop` - only exists if something invoked the function.
  It is not in state, so destroy leaves it. That is the drift finding; delete
  it by hand when you are done with the demo.
- Objects written into the S3 buckets after apply. `terraform destroy` fails
  on non-empty buckets - empty them first if the demo put anything in them.

This stack deliberately sets NO default tags -- several resources exist
precisely to be untagged, and a blanket tag would erase that finding.
Cleanup therefore relies on Terraform state, which covers every resource here.
Terraform state. To hunt stragglers after a destroy:

```sh
aws resourcegroupstaggingapi get-resources \
  --region us-east-1
```

Anything still listed is a leftover.
