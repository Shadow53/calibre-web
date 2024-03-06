
check:
    mypy .
    ruff check

style:
    black .

test:
    pytest
