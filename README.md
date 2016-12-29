## 페블용 한국어팩

언어팩 버전 4.0, 펌웨어 버전 4.3에 맞춰 번역중인 메세지 리스트입니다.
제안및 수정 PR 환영이예요~

### 언어팩 개발환경 설정


```bash
$ sudo easy_install pip
$ sudo pip install virtualenv

$ cd PebbleSDK-3.2.1/
$ virtualenv --no-site-packages .env
$ source .env/bin/activate
$ CFLAGS="" pip install -r requirements.txt
$ deactivate

$ tar xvfz arm-cs-tools.tgz
```

### Pack/Unpack

```bash
$ source set-env.sh
$ make c83

```
