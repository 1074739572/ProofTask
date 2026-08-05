# search_service

Product catalog with a search function.

## Search contract (from the product owner)

- Match against product `name` **OR** `category`
- Case-insensitive
- Return at most **10** results
- Empty query returns an empty list

## Commands

- Run tests: `python -m pytest tests -q`

## Layout

- `app.py` — products + search
- `tests/test_app.py` — basic tests
