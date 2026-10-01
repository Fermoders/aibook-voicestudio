# AIBook XTTS v2

Старая версия на Coqui XTTS v2 сохранена отдельно от VoiceStudio.
Готовая папка: `dist\AIBook Portable`, запуск: `AIBook.cmd`.
Внутри находятся Python 3.11, Tk, PyTorch CUDA, FFmpeg и модель XTTS v2.

Для исходников: `start_aibook.cmd`. Повторная установка: `install_aibook.cmd`.
Установщик XTTS использует Python 3.11 и локальную `.venv`.

Поддерживаются TXT, FB2, EPUB, DOCX и текстовые PDF; результат WAV/MP3.
Доступны встроенный голос, клонирование, обе скорости, тембр и громкость.
Выбирается папка результата; существующие файлы не перезаписываются.
После остановки повторный запуск продолжает работу по кэшу фрагментов.

XTTS v2 распространяется по Coqui Public Model License (CPML).
Для принятия лицензии и подготовки модели:

```powershell
.\.venv\Scripts\python.exe .\main.py --accept-cpml
```

```powershell
.\.venv\Scripts\python.exe .\main.py --text "Проверка русской озвучки." --output .\outputs\test.wav --speaker "Ana Florence" --device auto
```

Настройки и кэш исходной версии: `%LOCALAPPDATA%\AIBookXTTS`.
В portable-версии данные находятся в `data\aibook`, результаты в `outputs`.
Для клонирования используйте разрешённую чистую запись одного человека
без музыки и реверберации длительностью 6-15 секунд.
