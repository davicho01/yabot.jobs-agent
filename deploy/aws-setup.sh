#!/usr/bin/env bash
# One-time: the IAM role the logo agent's workflow assumes via GitHub OIDC.
# It may only upload new content-addressed logos (logos/*, never the live
# logos/c/* aliases) to the frontend bucket. Run from the repo root.
set -euo pipefail

ROLE=yabot-logo-agent

aws iam create-role --role-name "$ROLE" \
  --assume-role-policy-document file://deploy/trust-policy.json \
  --description "yabot.jobs-agent GitHub workflow: upload generated logos"
aws iam put-role-policy --role-name "$ROLE" --policy-name logo-upload \
  --policy-document file://deploy/logo-upload-policy.json

ARN=$(aws iam get-role --role-name "$ROLE" --query Role.Arn --output text)
gh secret set AWS_ROLE_ARN --repo davicho01/yabot.jobs-agent --body "$ARN"
echo "Created $ARN and stored it as AWS_ROLE_ARN."
