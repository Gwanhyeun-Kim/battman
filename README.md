# 배터리 관리자

맥북의 배터리 상태를 기록하고 급방전, 높은 CPU 사용량, 잠자기 중 방전을 확인하는 macOS 앱입니다. 수집한 데이터는 각 Mac의 `~/.config/battman/`에 저장됩니다.

## 설치

`release/BatteryManager-1.0.pkg`를 열어 설치하세요. macOS에서 개발자를 확인할 수 없다는 경고가 나오면 Finder에서 패키지를 control-클릭하고 **열기**를 선택합니다. 계속 차단되면 시스템 설정의 **개인정보 보호 및 보안**에서 **그래도 열기**를 선택하세요.

macOS 13 Ventura 이상을 지원하며, Apple Silicon과 Intel Mac용 바이너리를 모두 포함합니다. 이 앱은 Apple 개발자 인증서 서명 및 공증을 받지 않았기 때문에 첫 실행 때 macOS의 보안 확인을 직접 허용해야 합니다.

자세한 단계는 [`release/INSTALL-KO.txt`](release/설치%20안내.txt)를 참고하세요.

## 소스에서 패키지 만들기

Xcode Command Line Tools가 설치된 Mac에서 아래 명령을 실행하면 `dist/배터리관리자-1.0.pkg`를 만듭니다.

```sh
bash src/build_pkg.sh
```

빌드 스크립트는 arm64와 x86_64 실행 파일을 만들고 설치 패키지의 구성과 앱 서명을 확인합니다.
