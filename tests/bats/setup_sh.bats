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
