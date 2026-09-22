@test "setup.sh prints usage and exits 2 with no args" {
  run bash "$BATS_TEST_DIRNAME/../../setup.sh"
  [ "$status" -eq 2 ]
  [[ "$output" == *"usage"* ]]
}
@test "setup.sh refuses to run without AWS creds in env" {
  ( unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_PROFILE
    run bash "$BATS_TEST_DIRNAME/../../setup.sh" my-bucket us-east-1
    [ "$status" -ne 0 ]
    [[ "$output" == *"admin"* ]] )
}

@test "setup.sh prints the role ARN and the permissions level, no extra-buckets policy ARN" {
  stub="$(mktemp -d)"
  cat > "$stub/tofu" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "output" ] && [ "$2" = "-raw" ]; then
  case "$3" in
    bucket_admin_role_arn) echo "arn:aws:iam::123456789012:role/backup-engine-bucket-admin" ;;
    permissions_level) echo "3" ;;
    *) echo "val-$3" ;;
  esac
fi
exit 0
EOF
  chmod +x "$stub/tofu"
  export PATH="$stub:$PATH" AWS_PROFILE=stub
  run bash "$BATS_TEST_DIRNAME/../../setup.sh" my-bucket us-east-1
  rm -rf "$stub"
  [ "$status" -eq 0 ]
  [[ "$output" == *"BUCKET_ADMIN_ROLE_ARN=arn:aws:iam::123456789012:role/backup-engine-bucket-admin"* ]]
  [[ "$output" == *"PERMISSIONS_VERSION=3"* ]]
  [[ "$output" != *"RUNTIME_EXTRA_BUCKETS_POLICY_ARN"* ]]
  [[ "$output" != *"runtime_extra_buckets_policy_arn"* ]]
}
