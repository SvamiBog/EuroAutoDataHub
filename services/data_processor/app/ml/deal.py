"""Deal score (этап 5.2): где цена объявления в распределении цен похожих автомобилей.

По квантилям модели P10/P50/P90 log-цены строится асимметричное нормальное распределение: «сигма» вниз —
(P50 − P10) / 1,2816, вверх — (P90 − P50) / 1,2816. z — отклонение log-цены от P50 в этих сигмах,
доля более дешёвых похожих объявлений — Φ(z), deal score = 100 · (1 − Φ(z)): 90 — дешевле 90 % похожих
объявлений (цена на уровне P10), 50 — по справедливой цене, 10 — дороже 90 %.
"""
import math

from app.ml.price_model import Z90

# Минимальная «сигма» log-цены: иначе при очень узком интервале любая разница была бы огромным z
MIN_SIGMA = 0.02


def interval_z(log_price: float, log_p10: float, log_p50: float, log_p90: float) -> float:
    diff = log_price - log_p50
    sigma = (log_p50 - log_p10) / Z90 if diff < 0 else (log_p90 - log_p50) / Z90
    return diff / max(sigma, MIN_SIGMA)


def normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def deal_score(z: float) -> float:
    return round(100.0 * (1.0 - normal_cdf(z)), 1)
