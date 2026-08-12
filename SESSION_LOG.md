# Sesi Log — 12 Agustus 2026

Ringkasan percakapan sesi ini untuk resume kerja berikutnya. Proyek: **freelance-lead-gen**
(bot otomatisasi screening lowongan freelance + pembuatan draft outreach).

## Status akhir sesi

- **446/446 test lulus** (termasuk 4 test fitur resume baru)
- DB direset bersih + query ditambah keyword web3
- Discovery baru: **100 leads** dari remote_ok
- Screening: 100 → **3 qualified** (skor 54–70): Java Developer (Clera), Senior React
  Fullstack (Lemon.io), owlette (Tridant)
- Draft: **3 dibuat**, semua **lolos verifikasi anti-AI** (skor 90–96)
- State DB terakhir: `discovered 0`, `qualified 0`, `drafted 3`, `reviewed 0`, `rejected 97`
- **Menunggu review HITL**: 3 draf

## Perubahan kode sesi ini

1. **Fitur resume** — `src/freelance_lead_gen/agents/orchestrator.py` + `cli.py`:
   - `pipeline --no-discover` kini memuat lead `DISCOVERED` + `QUALIFIED` dari DB dan
     melanjutkan dari titik berhenti (sebelumnya mulai dari nol).
   - Lead yang sudah qualified **tidak diskor ulang** (lewat filtering lagi), langsung ke drafting.
   - `_load_pending_opportunities()` helper + 4 test baru di `tests/test_integration_pipeline.py`.
   - Committed: `2d45b4d`, pushed ke fork `muhdawam94/freelance-lead-gen`,
     PR dibuka: **https://github.com/iknowkungfubar/freelance-lead-gen/pull/40**

## Keputusan pengguna sesi ini

1. Platform berbayar (Upwork/LinkedIn/Freelancer) **di-skip** dulu, fokus platform gratis
   (remote_ok + yc_work) → `PLATFORMS_ENABLED=remote_ok,yc_work`.
2. Login via Gmail **tidak bisa diotomatisasi** bot (bot hanya dukung username/password).
   `browser_data` kosong — belum ada sesi cookie. Solusi tersedia: login manual sekali via
   browser bot di `browser_data`, atau setel password platform biasa.
3. Query discovery ditambah keyword web3: `blockchain developer,smart contract,web3,defi,Solidity,dApp development`.

## Catatan penting untuk sesi berikutnya

- `browser_data` masih kosong — kalau mau aktivasi platform berbayar, lakukan login manual
  sekali lewat browser bot (non-headless), sesi cookie akan persist.
- `yc_work` API fetch gagal 406 tapi punya browser-fallback; belum menghasilkan leads.
- 3 draf menunggu review: `.\.venv\Scripts\python.exe -m freelance_lead_gen review`

---

# Sesi Log — 10 Agustus 2026

Ringkasan percakapan sesi ini untuk resume kerja besok. Proyek: **freelance-lead-gen**
(bot otomatisasi screening lowongan freelance + pembuatan draft outreach).

## Status akhir sesi

- Discovery: **100 lead** ditemukan
- Screening/kualifikasi: **4 lead tervalidasi** (tersimpan di DB)
- Draft: **0** — terblokir kuota harian Groq
- State DB terakhir: `discovered 0`, `qualified 4`, `drafted 0`

## Kronologi sesi

1. Pipeline dijalankan penuh (CLI `pipeline`): discovery 100 → filtering → 4 qualified.
2. Draft gagal semua dengan `403 Access denied. Please check your network settings.`
   — blokir abuse-protection **di sisi provider Groq** (bukan bug kode). Bahkan probe
   sederhana (`"Reply with the single word: ok"`) ikut kena 403.
3. Dikonfirmasi via riset web: error ini adalah blokir berbasis IP/network Groq
   (community report: request yang sama sukses lewat proxy/VPN). Bukan masalah API key.
4. Blokir pulih setelah ~25 menit. Probe kembali sukses.
5. Muncul `429 rate_limit_exceeded`: kuota harian gratis Groq (TPD 100.000 token) habis
   — `Used 99988 / 100000`. Sisa ~12 token, tidak cukup untuk satu draft (butuh ~1.400–2.300).
6. Error `400 json_schema not supported` pada `llama-3.3-70b-versatile` sudah **bukan blocker**:
   `client.py` punya fallback otomatis ke `json_object` yang terbukti berfungsi (klasifikasi
   sebelumnya sukses lewat jalur itu). Yang gagal murni karena kuota TPD.

## Perubahan kode sesi ini

- `src/freelance_lead_gen/llm/client.py:406`
  - 403 `"Access denied ... network settings"` kini dikenali sebagai blokir temporer provider
    dan diberi **backoff 60s** antar-retry (sebelumnya `2^attempt` detik: 2/4/8s, terlalu cepat
    untuk blokir berdurasi menit). Ini memungkinkan pipeline menunggu blokir mencabut
    alih-alih langsung menyerah.

## Skrip resume untuk besok pagi

Reset TPD Groq: **00:00 UTC ≈ 07:00 WIB**. Jalankan setelah itu:

```powershell
$env:PYTHONIOENCODING="utf-8"; .venv\Scripts\python.exe "$env:TEMP\opencode\resume_drafts.py"
```

- Skrip ini memuat lead berstatus `qualified` dari DB lalu menjalankan hanya fase
  personalization + verification (`run_discovery=False, run_filtering=False`), jadi
  tidak mengulang discovery/screening.
- `$env:TEMP\opencode\resume_drafts.py` bisa terhapus. Regenerasi cepat bila perlu:
  `await init_db()` → `repo.search(status=LeadStatus.QUALIFIED)` →
  `orchestrator.run_full_pipeline(opportunities=qualified, run_discovery=False,
  run_filtering=False)`.
- CATATAN: CLI `pipeline --no-discover` **tidak** memuat lead dari DB, jadi skrip di atas
  adalah jalur resume yang benar (bukan CLI).

## Keputusan pengguna sesi ini

1. Saat blokir 403: **"Tunggu lalu retry"** → blokir pulih ~25 menit.
2. Saat kuota TPD habis: **"Tunggu reset harian (besok pagi)"** → jalankan ulang skrip resume besok.

## Alternatif bila tidak mau menunggu reset

- Upgrade akun Groq ke **Dev tier** (console.groq.com/settings/billing) → cap TPD hilang.
- Ganti provider di `.env`: `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`
  (OpenAI-compatible, mis. OpenAI/OpenRouter/ollama).

## Alur pasca-draft

Setelah draft dibuat: verifikasi otomatis berjalan → review HITL via CLI (`review`/TUI)
sebelum dikirim. Bot **tidak pernah** mengirim otomatis; semua butuh persetujuan manusia.
