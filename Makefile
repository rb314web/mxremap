.PHONY: test rpm
test:
	python3 -m unittest discover -s tests -v
rpm:
	bash packaging/build-rpm.sh
