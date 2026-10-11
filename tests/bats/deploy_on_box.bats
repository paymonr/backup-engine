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

# carried_mounts: the wiring. Real Tower state (2026-10-10): the stray volume shows up BOTH in
# HostConfig.Binds (an earlier deploy passed it as --volume <id>:/backup/appdata) and in .Mounts.
VOL=1401ebc6a5f9e494af6610f26b13fa079446690e61b9102a951cf4c89971251f
# each case stubs volume_is_anonymous (no docker in tests) inside its own bash -c

@test "carried_mounts drops the stray anonymous volume from Binds and Mounts, keeps host binds" {
  run bash -c "source '$BATS_TEST_DIRNAME/../../tools/unraid/deploy_on_box.sh'; volume_is_anonymous() { [ \"\$1\" = $VOL ]; }; carried_mounts '/backup/media /config /cache' '$VOL:/backup/appdata' '/mnt/user:/backup/media:ro' '/mnt/user/appdata/backup-engine/config:/config' '/mnt/user/appdata/backup-engine/cache:/cache' -- '$VOL:/backup/appdata' 2>/dev/null"
  [ "$output" = "/mnt/user:/backup/media:ro
/mnt/user/appdata/backup-engine/config:/config
/mnt/user/appdata/backup-engine/cache:/cache" ]
}

@test "carried_mounts keeps an anonymous volume the new image still declares, once" {
  run bash -c "source '$BATS_TEST_DIRNAME/../../tools/unraid/deploy_on_box.sh'; volume_is_anonymous() { [ \"\$1\" = $VOL ]; }; carried_mounts '/cache' '$VOL:/cache' -- '$VOL:/cache' 2>/dev/null"
  [ "$output" = "$VOL:/cache" ]
}

@test "carried_mounts keeps a named volume and a mount not covered by a bind" {
  run bash -c "source '$BATS_TEST_DIRNAME/../../tools/unraid/deploy_on_box.sh'; volume_is_anonymous() { return 1; }; carried_mounts '' '/mnt/user:/backup/media:ro' -- 'mydata:/data' 2>/dev/null"
  [ "$output" = "/mnt/user:/backup/media:ro
mydata:/data" ]
}

@test "carried_mounts says on stderr which volume it drops" {
  run bash -c "source '$BATS_TEST_DIRNAME/../../tools/unraid/deploy_on_box.sh'; volume_is_anonymous() { return 0; }; carried_mounts '/config' 'abc:/backup/appdata' -- 2>&1 >/dev/null"
  [[ "$output" == *"not carrying anonymous volume abc"*"/backup/appdata"* ]]
}
