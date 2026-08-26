setup_common() {
  export CACHE_DIR="$BATS_TEST_TMPDIR/cache"
  export LOG_FILE="$CACHE_DIR/test.log"
  mkdir -p "$CACHE_DIR"
  # shellcheck source=/dev/null
  source "$BATS_TEST_DIRNAME/../../scripts/lib/common.sh"
}
