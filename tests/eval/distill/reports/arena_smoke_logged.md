# 老师擂台: smoke_logged

判定 710 次(按领域:biomed 150, firefox 150, legal 150, security 100, silent 60, theorem 100)

| 老师 | 作答 | noul AUC | Brier | ECE | 单选 top1 | 单选静默 | 选择 R / P / 静默 |
|---|---|---|---|---|---|---|---|
| real_jev-logged | 710 | 0.9692 | 0.0268 | 0.0738 | 83.9% | 100.0% | 44.6% / 82.0% / 100.0% |

## 分领域(noul AUC / 单选 top1 / 选择 R-P)

| 老师 | biomed | firefox | legal | security | silent | theorem |
|---|---|---|---|---|---|---|
| real_jev-logged | 0.9779 / 79.0% / 52.9%-82.7% | 0.9817 / 88.6% / 66.7%-96.2% | 0.9472 / 77.0% / 20.7%-56.4% | 0.9633 / 81.1% / 25.0%-67.4% | None / - / --- | 0.9925 / 91.3% / 53.0%-91.4% |
