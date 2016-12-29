all: c83 c83k


c83: 000
	cp ./templates/000 ./templates/C83/
	./bin/pbpack_tool.py pack ./packed/C83.pbl ./templates/C83/0*

c83k: 000
	cp ./templates/000 ./templates/C83k/
	./bin/pbpack_tool.py pack ./packed/C83k.pbl ./templates/C83k/0*

000:
	msgfmt ko_KR.po -o ./templates/000

