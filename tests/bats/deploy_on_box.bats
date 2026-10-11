# tools/unraid/deploy_on_box.sh carries the live container's volumes onto the new one. An anonymous
# volume whose destination the new image no longer declares must NOT be carried forward — that is how
# an empty /backup/appdata volume survived every deploy after the mapping was removed.
setup() {
  # shellcheck source=/dev/null
  source "$BATS_TEST_DIRNAME/../../tools/unraid/deploy_on_box.sh"
}

@test "sourcing deploy_on_box.sh does not run the deploy" {
  [ "$(type -t keep_volume)" = "function" ]
}

@test "keeps a named volume even when the image does not declare its destination" {
  run keep_volume mydata /data 0 "/config /cache"
  [ "$status" -eq 0 ]
}

@test "keeps an anonymous volume whose destination the new image still declares" {
  run keep_volume 1401ebc6a5f9 /cache 1 "/backup/media /config /cache"
  [ "$status" -eq 0 ]
}

@test "drops an anonymous volume whose destination the new image no longer declares" {
  run keep_volume 1401ebc6a5f9 /backup/appdata 1 "/backup/media /config /cache"
  [ "$status" -eq 1 ]
}

@test "drops an anonymous volume when the new image declares no volumes at all" {
  run keep_volume 1401ebc6a5f9 /backup/appdata 1 ""
  [ "$status" -eq 1 ]
}

@test "does not match a declared destination by prefix" {
  run keep_volume 1401ebc6a5f9 /backup 1 "/backup/media"
  [ "$status" -eq 1 ]
}
