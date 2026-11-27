# Unmodified PMC Cloud text fixture

The original text and metadata are unmodified. See the ARTICLE INFORMATION header for authors and the original license statement. Licensed CC BY.

- DOI 10.1371/journal.pone.0154431
- [PMC Cloud metadata](https://pmc-oa-opendata.s3.amazonaws.com/metadata/PMC4858230.1.json)
- [PMC Cloud text](https://pmc-oa-opendata.s3.amazonaws.com/PMC4858230.1/PMC4858230.1.txt)

The repository does not contain the article files. For the optional offline
regression suites, download these public copies into a directory outside the
repository and set `SELFCORPUS_VERIFIED_TEXT_FIXTURES` to that directory:

```sh
fixture_dir=$(mktemp -d)
base=https://pmc-oa-opendata.s3.amazonaws.com
curl -fLsS "$base/PMC4858230.1/PMC4858230.1.txt" -o "$fixture_dir/PMC4858230.1.txt"
curl -fLsS "$base/metadata/PMC4858230.1.json" -o "$fixture_dir/PMC4858230.1.json"
SELFCORPUS_VERIFIED_TEXT_FIXTURES="$fixture_dir" python -B -m unittest discover -s tests -v
```

The tests verify both SHA-256 hashes before reading fixture data. They do not
retrieve it over the network themselves. With no fixture path, only the two
fixture-dependent test classes are skipped.
