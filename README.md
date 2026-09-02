# nuitka_build.py

Tek dosyalık, yalnızca standart kütüphane kullanan bir araç: bir Python projesini (klasör ya da `.py` dosyası) verirsiniz, entry dosyasını bulur, bağımlılıkları kurar ve [Nuitka](https://nuitka.net) ile native, tek dosya (onefile) bir çalıştırılabilir üretir.

```bash
python nuitka_build.py ./projem                 # klasör ver, gerisini o halleder
python nuitka_build.py ./projem/main.py         # ya da doğrudan entry dosyası
python nuitka_build.py ./projem --run           # derle ve bir kez çalıştırarak doğrula
python nuitka_build.py ./projem --dry-run       # ne yapacağını göster, hiçbir şey çalıştırma
```

## Ne yapar

1. **Entry tespiti** (öncelik sırasıyla)
   - `--entry dosya.py` / `--entry paket` / `--entry paket.modul:fonksiyon`
   - `pyproject.toml` → `[project.scripts]`, `[project.gui-scripts]`, `[tool.poetry.scripts]`
   - `setup.py` / `setup.cfg` → `console_scripts`
   - `paket/__main__.py` (Nuitka'nın `--python-flag=-m` paket modu, `python -m paket` ile aynı)
   - Bilinen adlar: `main.py`, `app.py`, `cli.py`, `run.py`, `manage.py` ...
   - `if __name__ == "__main__":` içeren dosyaların puanlanması (tests/, docs/, examples/ cezalı)
2. **Bağımlılık toplama** (bulduğu her kaynağı birleştirir)
   - **Lock dosyaları önce**: `uv.lock` (`uv export --frozen`), `poetry.lock`, `pdm.lock`, `Pipfile.lock` → projenin gerçekten çalıştığı pinli sürümler kurulur; gevşek manifest kısıtları atlanır (`--no-lock` ile kapatılır)
   - PEP 723 inline script metadata (`# /// script`)
   - `pyproject.toml`: PEP 621 `dependencies` + `optional-dependencies` (`--extras ad`), Poetry (`^`/`~` dönüştürülür, git/path/markers/extras desteklenir), Hatch, `dependency-groups` (`--dev`)
   - `setup.py install_requires` (AST ile), `setup.cfg`
   - `Pipfile` / `Pipfile.lock`
   - `requirements.txt`, `requirements/base.txt` vb.; `--all-requirements` ile hepsi, `--dev` ile dev/test dosyaları da; `constraints.txt` otomatik `-c`
   - `environment.yml` (conda) → `pip:` bölümü + conda paket adları (best effort)
   - Hiçbir manifest yoksa: import taraması + import-adı→PyPI-adı tablosu (uyarı verir; `--no-infer` ile kapatılır)
3. **İzole ortam**: `<proje>/.nuitka-build/venv`. Nuitka + `ordered-set` + `zstandard` ve tüm bağımlılıklar buraya kurulur; Nuitka bu yorumlayıcıyla çalıştırılır.
   - **uv otomatik gelir**: PATH'te yoksa `~/.cache/nuitka_build/tools/` altındaki küçük bir araç ortamına `pip install uv` yapılır. pip'in çözümleyicisi büyük projelerde (`resolution-too-deep`) pes eder, uv etmez. `--installer pip` ile eski davranış.
   - **`requires-python` uygulanır**: `pyproject.toml`/`uv.lock` "3.12" istiyorsa host 3.11 olsa bile önce PATH'te `python3.12` aranır, yoksa uv uygun CPython'u indirir (başlık dosyaları dahil; Nuitka bu "Python Build Standalone" sürümüyle macOS ve Rocky'de doğrulandı). `--python` ile elle seçebilirsiniz.
4. **Otomatik Nuitka ayarları**
   - **Django profili** (`manage.py` varsa): entry `manage.py` olur (`binary migrate`, `binary runserver`, özel komutlar); settings modülü `manage.py`'den okunur; `INSTALLED_APPS` ve settings'teki tüm noktalı string'ler (middleware, backend, storage, task yolları) disk üzerinde çözümlenip `--include-package`/`--include-module` olarak verilir; `django` paketi komple dahil edilir ve derleme sırasında `DJANGO_SETTINGS_MODULE` ayarlanır. Nuitka'nın kendi `--module-parameter=django-settings-module` desteği bilerek kullanılmaz: settings'i derleme anında import edip değerlendiriyor ve Saleor'da çöküyor; Nuitka'nın bu parametre için verdiği `options-nanny` uyarısı beklenen ve zararsızdır. `--no-django` ile kapatılır, `--django-settings` ile modül seçilir.
   - **Runtime import sağlaması**: kurulumdan sonra projenin (ve tutulan test paketlerinin) `try/except` veya `TYPE_CHECKING` ile korunmamış tüm üçüncü parti import'ları venv'de aranır. Eksik varsa dev bağımlılık grubu kurulur (Saleor'da tutulan test fixture'ları `pytest`, `freezegun`, `fakeredis`, `vcr` ister); hâlâ eksik kalanlar uyarıyla listelenir. Bu, "binary açılışta `ModuleNotFoundError` veriyor" sürprizini derlemeden önce yakalar.
   - **Test paketleri dışlanır, ama akıllıca**: projedeki tüm `tests`/`test` paketleri bulunur; runtime kodun (göreli import'lar dahil, geçişli olarak) import ettikleri tutulur, geri kalanı `--nofollow-import-to` ile tek tek dışlanır. Saleor'da 68 test paketinden 60'ı dışlanıyor, `createsuperuser` ve `random_data`'nın kullandığı 8'i kalıyor. `--keep-tests` hepsini derler.
   - Plugin tespiti: `tk-inter`, `pyside6/2`, `pyqt5/6`, `matplotlib`, `gevent`, `kivy`, `pywebview`, `spacy`, `playwright`, `glfw`, `dill-compat` ...
   - Veri klasörleri: `assets/`, `data/`, `static/`, `templates/`, `resources/`, `locale/` ... (içinde `.py` yoksa) → `--include-data-dir`
   - Projenin kendi paketleri → `--include-package`
   - src-layout (`src/paket`) için `PYTHONPATH` ayarı
   - `modul:fonksiyon` entry'leri için `.nuitka-build/launcher/` altında küçük bir başlatıcı üretilir
5. **Çıktı**: `<proje>/dist/<ad>` (Windows'ta `.exe`, macOS `--gui` ile `.app`), XML derleme raporu ve `build-info.json` `.nuitka-build/` altında.

## Sık kullanılan seçenekler

| Seçenek | Açıklama |
|---|---|
| `--mode onefile\|standalone\|app\|accelerated` | Varsayılan `onefile`. Sorun ayıklarken önce `standalone` deneyin. |
| `--name AD` | Çıktı adı (varsayılan: script adı / proje adı) |
| `-o DIZIN` | Çıktı dizini (varsayılan `<proje>/dist`) |
| `--python 3.12` veya `--python /yol/python` | Derleme ortamının yorumlayıcısı |
| `--gui` | Windows'ta konsol yok, macOS'ta `.app` bundle |
| `--icon dosya` | `.ico` (Win) / `.icns`,`.png` (mac) / `.png` (Linux) |
| `--extras ad`, `--dev`, `--req dosya`, `--extra-req spec` | Bağımlılık kaynaklarını genişletir |
| `--no-lock`, `--installer uv\|pip`, `--no-build` | Lock dosyasını yoksay; kurucu seç; ortamı hazırla ama derleme |
| `--no-django`, `--django-settings MODUL`, `--keep-tests` | Django profili ve test paketleri |
| `--plugin ad`, `--no-auto-plugins` | Plugin kontrolü |
| `--data-dir SRC[=DST]`, `--no-auto-data`, `--package-data` | Veri dosyaları |
| `--lto`, `--jobs N`, `--clang`, `--mingw64`, `--low-memory` | Derleyici ayarları |
| `--onefile-cache` | Onefile açılımını sabit bir önbellek dizininde tut (daha hızlı açılış, daha az AV/firewall sorunu) |
| `--deployment` | Yayın derlemesi (Nuitka'nın geliştirme zamanı korumalarını kapatır) |
| `--clean`, `--no-install`, `--installer uv\|pip` | Ortam yönetimi |
| `--run --run-args "..."` | Derleme sonrası duman testi |
| `-- <nuitka argümanları>` | `--` sonrası her şey Nuitka'ya aynen geçer |

## Gereksinimler

- Host'ta Python 3.8+ (3.11 altı için TOML ayrıştırma adına `pip install tomli` önerilir).
- C derleyici: macOS'ta Xcode Command Line Tools (`xcode-select --install`), Linux'ta gcc/clang (`build-essential python3-dev`), Windows'ta MSVC ya da Nuitka'nın kendisinin indirdiği MinGW64 (`--assume-yes-for-downloads` otomatik verilir).
- Nuitka çapraz derleme yapmaz: Windows `.exe` için Windows'ta, Linux binary'si için Linux'ta çalıştırın. Linux'ta taşınabilirlik için desteklemek istediğiniz en eski dağıtımda derleyin.

## Rocky Linux / RHEL ve Docker

**Host (Rocky 8 veya 9):** varsayılan `python3` Rocky 8'de 3.6 (ya da hiç yok), Rocky 9'da 3.9'dur. Script host tarafında Python 3.8+ ister; gerisini kendisi halleder:

```bash
sudo dnf install -y gcc gcc-c++ make git        # Rocky 9: python3 (3.9) zaten var
sudo dnf install -y python3.11                  # Rocky 8'de gerekli (3.6 çok eski)
sudo dnf install -y ccache patchelf             # opsiyonel (EPEL gerekebilir); script patchelf'i pip'ten de kurar
python3 nuitka_build.py ./projem --run
```

- `python3.X-devel` artık şart değil: dağıtım Python'unda `Python.h` yoksa script uv'nin başlık dosyalarıyla gelen CPython'unu indirip onu kullanır (`~/.local/share/uv/python`, ya da `--cache-dir` altı). İsterseniz `dnf install python3.12-devel` kurup `--python python3.12` ile sistem Python'unu zorlayabilirsiniz.
- Host Python 3.11 altıysa (`tomllib` yok) script `~/.cache/nuitka_build/tools/` altındaki araç ortamına `tomli` + `uv` kurar ve kendini o yorumlayıcıyla yeniden başlatır; `pyproject.toml`/`Pipfile`/PEP 723 böylece 3.9 ile de okunur.
- Linux'ta standalone/onefile için `patchelf` gerekir; script `patchelf` pip paketini otomatik kurar ve venv'in `bin/` dizinini Nuitka çalışırken `PATH`'e ekler.
- Üretilen binary derlendiği makinenin glibc'sine bağlıdır: Rocky 9'da (glibc 2.34) derlenen binary Rocky 8'de (glibc 2.28) çalışmaz, tersi çalışır. Desteklemek istediğiniz **en eski** dağıtımda derleyin.

**Dockerfile şart değil.** Script tek başına çalışır; Dockerfile yalnızca hazır bir imaj kısayolu. Çıplak bir Rocky container'ında da aynı şey geçerlidir:

```bash
docker run --rm -v "$PWD/projem:/src/projem" -v "$PWD/nuitka_build.py:/opt/nuitka_build.py:ro" \
  rockylinux:9 bash -c 'dnf -y install gcc gcc-c++ make python3-devel && \
                        python3 /opt/nuitka_build.py /src/projem --venv /tmp/venv --run'
```

İki not: container'da `--venv` (ya da imajdaki ortam değişkenleri) ile sanal ortamı bind mount dışına alın, ve `rockylinux:8` imajında `python3` hiç kurulu değildir (`dnf install python3.11 python3.11-devel` gerekir).

**Hazır imaj:** repo içindeki `Dockerfile` (`rockylinux:9`, `--build-arg ROCKY=8` ile 8) hazır bir builder imajı verir:

```bash
docker build -t nuitka-builder .                      # Rocky 8 için: --build-arg ROCKY=8
docker run --rm -v "$PWD/projem:/src/projem" nuitka-builder /src/projem --run
# çıktı: projem/dist/projem   (Rocky 9 imajında derlenmiş ELF binary)

# venv ve önbellekleri named volume ile kalıcı yapın (ikinci derleme çok daha hızlı):
docker run --rm -v "$PWD/projem:/src/projem" \
  -v nuitka-venv:/opt/nuitka-venv -v nuitka-cache:/opt/nuitka-cache \
  nuitka-builder /src/projem --name projem --run
```

Container'a özgü davranışlar:
- Dockerfile `NUITKA_BUILD_VENV=/opt/nuitka-venv` ve `NUITKA_BUILD_CACHE_DIR=/opt/nuitka-cache` ayarlar; script bu ortam değişkenlerini `--venv` / `--cache-dir` varsayılanı olarak kullanır. Böylece sanal ortam bind mount'un **dışında** kalır (Docker Desktop'ta mount içinde venv oluşturmak symlink sorunları çıkarır, native Linux'ta da gereksiz yavaştır).
- Host'ta oluşturulmuş `.nuitka-build/venv` container'da çalışmaz (farklı yorumlayıcı yolu); script bunu fark edip ortamı otomatik yeniden kurar. Aynı durum tersi yönde de geçerlidir.
- Projeyi `/src` gibi genel bir ada mount ederseniz binary adı türetilemez; `--name` verin ya da yukarıdaki gibi `/src/projem` olarak mount edin.
- `HOME` yazılabilir değilse (ör. `--user` ile çalıştırma) önbellekler `.nuitka-build/cache` altına düşer.
- Root olarak çalışırsanız `dist/` altındaki binary root'a ait olur; `docker run --user "$(id -u):$(id -g)" ...` ile kendi kullanıcınızla çalıştırabilirsiniz (bu durumda `/opt/nuitka-*` yazılabilir olmalı: volume'ları önceden oluşturup sahipliğini verin ya da `--venv /tmp/venv --cache-dir /tmp/cache` geçin).
- Onefile binary çalışırken kendini geçici bir dizine açar; `/tmp` `noexec` bağlıysa `-- --onefile-tempdir-spec=/var/tmp/{PRODUCT}` gibi çalıştırılabilir bir yer gösterin.
- Nuitka çapraz derlemez: imaj hangi mimaride çalışıyorsa (x86_64 / aarch64) binary o mimari içindir. Apple Silicon'da x86_64 binary için `docker run --platform linux/amd64 ...` (emülasyon, yavaş).

Eksik derleyici/başlık dosyası durumunda script kuruluma hiç başlamadan durur ve dağıtıma uygun `dnf`/`apt` komutunu yazar; `--skip-checks` ile bu kontrolü atlayabilirsiniz.

Doğrulanan senaryolar (aarch64, Docker):

| Ortam | Sonuç |
|---|---|
| `rockylinux:9`, hiçbir kurulum yok | derleyici eksik diye kurulumdan önce temiz hata + `dnf` komutu (çıkış 2) |
| `rockylinux:9` + `gcc gcc-c++ make python3-devel`, Dockerfile yok, Python 3.9 | ✅ tomli bootstrap, PEP 723 ve pyproject okundu, ELF binary çalıştı |
| `rockylinux:9` Dockerfile imajı (python3.11 + uv) | ✅ |
| `rockylinux:8` + `python3.11 python3.11-devel gcc`, Dockerfile yok | ✅ (glibc 2.28, Rocky 9'da da çalışır) |
| `rockylinux:8`, Python 3.6 | "3.8+ gerekiyor" mesajı ve `dnf` ipucu |

## Büyük bir Django projesi: Saleor örneği

`python nuitka_build.py ./saleor` ile (Saleor 3.24, Django 5.2, 227 paketlik `uv.lock`, 4320 `.py` dosyası) macOS arm64'te:

| Aşama | Süre / sonuç |
|---|---|
| Ortam: uv bootstrap, CPython 3.12 indirme, lock'tan 142 paket | ~20 s |
| Nuitka Python düzeyi derleme (Django + Celery + Saleor, testler hariç) | ~4 dk |
| C derlemesi (7993 dosya, clang, 6 iş) + linkleme + onefile sıkıştırma | ~30 dk (ikinci derlemede ccache sayesinde daha kısa) |
| Çıktı | `dist/saleor`, 154 MB onefile (650 MB dist, %25'e sıkıştırılmış) |

Öğrenilenler:
- **Sistem kütüphaneleri paketlenmez.** `python-magic`, `libmagic`'i `ctypes` ile sistemden yükler; binary'nin çalışacağı makinede de kurulu olmalı (macOS `brew install libmagic`, Rocky `dnf install file-libs`). Aynısı `psycopg[binary]` dışındaki C bağımlılıkları için de geçerli olabilir; Nuitka derleme raporu (`.nuitka-build/*-compilation-report.xml`) hangi `.so`/`.dylib`'lerin dahil edildiğini listeler.
- **Onefile her çalıştırmada açılır.** 650 MB'lık dist her başlatmada geçici dizine çıkarılır (bu Mac'te ~15 s). Sunucu uygulamaları için `--mode standalone` (klasör dağıtımı) ya da `--onefile-cache` (sabit önbellek dizini, ikinci açılış hızlı) kullanın.
- **Ortam değişkenleri aynen gerekir.** Binary `manage.py` gibi davranır: `SECRET_KEY`, `DATABASE_URL`, `REDIS_URL` vb. olmadan çalışmaz. Örnek: `SECRET_KEY=... DATABASE_URL=postgres://... ./dist/saleor migrate`.
- Django'nun string'le yüklediği her şey (`INSTALLED_APPS`, middleware, storage backend'leri, Celery task yolları) settings'ten çıkarılıp `--include-*` olarak verildiği için ek bayrak gerekmedi.

## Bilinen sınırlamalar

- `runpy`/`importlib` ile dinamik yüklenen eklentiler otomatik bulunmaz; `--include-package` (ya da `-- --include-module=...`) ile ekleyin.
- Nuitka `.py` dosyalarını veri olarak paketlemez; veri klasöründe kod varsa klasör atlanır.
- macOS `.app` bundle modunda `--output-filename` Nuitka tarafından yok sayılır; script bundle'ı derleme sonrası `<ad>.app` olarak yeniden adlandırır.
- Poetry `||` (VEYA) kısıtları PEP 440'a çevrilemez; ilk alternatif alınır.
