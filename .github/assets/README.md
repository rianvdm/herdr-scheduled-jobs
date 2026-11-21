# Repository artwork

- `logo.svg`: the logo at the top of the main README. It's a clock face whose hour marks are run dots: hours 12 to 4 have run, the ring at 5 is the next run, and the hour hand points toward it. The dark tile keeps the same contrast on light and dark pages.
- `social-preview.svg`: the source for `social-preview.png`, the 1280 × 640 image for GitHub's social preview (Settings → General → Social preview). Its logo is a copy of `logo.svg`, so change both together.

The preview's text uses the system font, so render the PNG on macOS. From this folder, with Google Chrome installed:

```bash
tmp=$(mktemp -d)
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless --hide-scrollbars \
  --user-data-dir="$tmp/profile" --window-size=1280,640 \
  --screenshot="$tmp/out.png" "file://$PWD/social-preview.svg" &&
  sips -g pixelWidth -g pixelHeight "$tmp/out.png" | grep -q 'pixelWidth: 1280' &&
  sips -g pixelHeight "$tmp/out.png" | grep -q 'pixelHeight: 640' &&
  [ "$(stat -f %z "$tmp/out.png")" -lt 1000000 ] &&
  mv "$tmp/out.png" social-preview.png && echo "Rendered social-preview.png"
rm -rf "$tmp"
```

It replaces `social-preview.png` only when the new render is 1280 × 640 and under 1 MB.
