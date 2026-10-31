# Contributing to jbd-up

Thanks for helping make the UP16S readable for everyone.

## The most valuable contribution: a capture

If you own a JBD UP-series pack (Eco-Worthy, Datou Boss, Vatrer rack units,
anything with a `UP16Sxxx` BMS), the single most useful thing you can do is a
raw capture:

```sh
jbd-up dump --port /dev/ttyUSB0 --seconds 30 --out my-pack.capture.txt
```

Open an issue titled `capture: <brand> <model> fw <version>` and attach the
file plus a photo of the BMS label if you have one. Captures land in
`tests/fixtures/` with your attribution and become regression tests.

## Code changes

1. Fork, branch, `pip install -e .[dev]`.
2. `ruff check . && ruff format --check . && pytest` must pass.
3. Any new protocol field must cite its source in `docs/PROTOCOL.md` and be
   tagged CONFIRMED or INFERRED. Add or extend a fixture test.
4. Keep the reader read-only by default. Write paths (0x79) need an explicit
   opt-in flag and a test.

## Developer Certificate of Origin (DCO)

This project uses the [DCO](https://developercertificate.org/) instead of a
CLA. By signing off you certify that you wrote the change or have the right to
submit it under the Apache-2.0 license.

Add a sign-off to every commit:

```sh
git commit -s -m "frame: decode 0x2810 product information"
```

which appends `Signed-off-by: Your Name <you@example.com>`. Unsigned commits
cannot be merged.

## Licence

By contributing you agree your contributions are licensed under Apache-2.0
(see `LICENSE` and `NOTICE`).
