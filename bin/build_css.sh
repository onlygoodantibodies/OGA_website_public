#!/usr/bin/env sh
#
# Build the pipeline's stylesheet.
#
# Run by Render's Build Command, between `pip install` and `collectstatic`, so
# the built file is on disk before static files are collected:
#
#   pip install -r requirements.txt && ./bin/build_css.sh \
#     && python manage.py collectstatic --noinput
#
# and by hand, once, if you want styling on a local `runserver` without the CDN.
#
# The output is NOT in git. That is the whole point of building it here: a
# committed stylesheet has to be rebuilt by whoever edits a template, and the
# time it is forgotten the page just quietly renders unstyled. Built on every
# deploy, it cannot drift from the templates it was built from.
#
# Failure here fails the build, which aborts the deploy and leaves the running
# version alone. That is the safe direction — the app falls back to the CDN when
# the file is absent, and `manage.py check` says so (pipeline.W003), so a build
# that silently did nothing is visible rather than merely survivable.
set -eu

# Pinned. cdn.tailwindcss.com serves Tailwind 3, so this is the version whose
# output matches what the app has been rendering; v4 is a different config
# format and a different set of defaults, and moving to it is its own decision.
VERSION="${TAILWIND_VERSION:-v3.4.17}"
BIN_DIR=".tailwind"
BIN="$BIN_DIR/tailwindcss-$VERSION"

CONFIG="tailwind.config.js"
INPUT="pipeline/assets/tailwind.css"
OUTPUT="pipeline/static/pipeline/tailwind.css"

if [ ! -x "$BIN" ]; then
    case "$(uname -s)-$(uname -m)" in
        Linux-x86_64)   ASSET="tailwindcss-linux-x64" ;;
        Linux-aarch64)  ASSET="tailwindcss-linux-arm64" ;;
        Darwin-x86_64)  ASSET="tailwindcss-macos-x64" ;;
        Darwin-arm64)   ASSET="tailwindcss-macos-arm64" ;;
        *)
            echo "build_css: no Tailwind CLI published for $(uname -s)-$(uname -m)." >&2
            echo "build_css: set TAILWIND_BIN to one you have, or build elsewhere." >&2
            exit 1
            ;;
    esac
    mkdir -p "$BIN_DIR"
    URL="https://github.com/tailwindlabs/tailwindcss/releases/download/$VERSION/$ASSET"
    echo "build_css: fetching $ASSET $VERSION"
    # -f so an HTML error page is never saved as a binary and then run.
    curl -fsSL --retry 3 --retry-delay 2 -o "$BIN" "$URL"
    chmod +x "$BIN"
fi

mkdir -p "$(dirname "$OUTPUT")"
"${TAILWIND_BIN:-$BIN}" --config "$CONFIG" --input "$INPUT" --output "$OUTPUT" --minify

# A build that produces nothing is the failure this whole file exists to make
# loud: an empty or missing stylesheet reaches the browser as an unstyled app.
if [ ! -s "$OUTPUT" ]; then
    echo "build_css: $OUTPUT is missing or empty after the build." >&2
    exit 1
fi
echo "build_css: wrote $OUTPUT ($(wc -c < "$OUTPUT") bytes)"
