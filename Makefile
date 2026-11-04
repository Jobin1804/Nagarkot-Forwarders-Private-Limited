PY ?= python3
.PHONY: install seed run test scale demo
install: ; $(PY) -m pip install -r requirements.txt
seed:    ; $(PY) -m app.seed
run:     ; $(PY) -m uvicorn app.main:app --reload
test:    ; $(PY) -m pytest -q
scale:   ; $(PY) -m app.scale
demo:    ; ./demo.sh
