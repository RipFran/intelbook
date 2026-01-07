# IntelX Phonebook CLI

Python CLI tool to query **IntelX Phonebook** and extract:

- Email addresses
- Domains
- URLs

It supports a single domain or a file containing multiple domains, writes per-domain outputs, and also produces merged output files in the root output folder.

## Documentation references

- Official IntelX API docs: https://help.intelx.io/docs/api/
- API PDF manual: https://www.ginseg.com/wp-content/uploads/sites/2/2019/07/Manual-Intelligence-X-API.pdf

## Requirements: Paid account for Phonebook

**Phonebook API access requires a paid IntelX account.**

> [!IMPORTANT]
> This tool was developed and tested using a **paid IntelX account**.  
> To run Phonebook queries you need:
> - An **API key**, obtainable from: https://intelx.io/account?tab=developer  
> - The paid API base URL: `https://2.intelx.io`

## Why this exists

IntelX Phonebook is designed to return selectors (emails/domains/URLs) associated with a given selector term (commonly a domain). The recommended flow is:

1. Start a phonebook job: `POST /phonebook/search`
2. Poll results: `GET /phonebook/search/result` until completion

IntelX requires:
- Authentication via API key (recommended via the `x-key` header)
- A valid `User-Agent` identifying your application
- Respecting rate limits (default guidance is **no more than 1 request per second** unless your license states otherwise)

## Installation

```bash
python -m venv .venv
# Windows:
# .venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
````

## Usage

### Single domain (default: emails + domains + urls)

```bash
python intelbook.py --api-key YOUR_INTELX_KEY --input example.com
```

### Only emails and URLs

```bash
python intelbook.py --api-key YOUR_INTELX_KEY --input example.com --types emails,urls
```

### Multiple domains from file

Create `domains.txt`:

```txt
example.com
example.org
# comments are allowed
sub.example.net
```

Run:

```bash
python intelbook.py --api-key YOUR_INTELX_KEY --input domains.txt --types emails,domains,urls
```

### Environment variable (optional)

```bash
export INTELX_API_KEY="YOUR_INTELX_KEY"
python intelbook.py --input example.com
```

## Output structure

By default, the tool writes into `./output`:

```
output/
  example.com/
    emails.txt          (optional)
    domains.txt         (optional)
    urls.txt            (optional)

  example.org/
    ...

  emails_all.txt        (optional, merged)
  domains_all.txt       (optional, merged)
  urls_all.txt          (optional, merged)
```

* Each per-domain folder is named after the input domain.
* The `*_all.txt` files are deduplicated merges across all processed domains.

## Operational notes

* `--rps` controls the maximum request rate (defaults to 1.0 req/s).
* `--result-limit` controls how many records are requested per polling call.
* `--maxresults` is the phonebook “maxresults” parameter (default 10000).
* `--json-debug` can be enabled to store raw JSON records per query under each domain folder.

## Exit codes

* `0`: Success
* `2`: Invalid CLI usage or missing inputs
* Non-zero runtime exceptions may also occur if the API returns errors (e.g., 401 unauthorized, 402 credits exhausted).
