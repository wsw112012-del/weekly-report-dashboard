@echo off
chcp 65001 >nul
cd /d %~dp0

echo [%date% %time%] 자동 수집 시작
python collect_보도자료.py 데이터
python collect_보도자료.py 페이먼트
python collect_보도자료.py AML
echo [%date% %time%] 자동 수집 완료
