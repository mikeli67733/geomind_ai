# -*- coding: utf-8 -*-
"""Unit tests for core/algos.py — pure numpy algorithm cores."""
import numpy as np
import pytest

from geomind_ai.core.algos import (
    compute_spectral_index,
    convolve3x3,
    kmeans_labels,
    compute_area_statistics,
)


class TestSpectralIndex:
    def test_ndvi_formula(self):
        # NDVI = (b1 - b2) / (b1 + b2); b1=NIR, b2=RED -> positive
        b1 = np.array([[0.5, 0.1]], dtype=np.float32)
        b2 = np.array([[0.1, 0.3]], dtype=np.float32)
        out = compute_spectral_index(b1, b2, None, "ndvi")
        expected = (b1 - b2) / (b1 + b2)
        assert np.allclose(out, expected, atol=1e-6)
        assert out[0, 0] > 0

    def test_ndvi_zero_denominator_is_nan(self):
        b1 = np.zeros((2, 2), dtype=np.float32)
        b2 = np.zeros((2, 2), dtype=np.float32)
        out = compute_spectral_index(b1, b2, None, "ndvi")
        assert np.isnan(out).all()

    def test_savi_formula(self):
        b1 = np.array([[0.2]], dtype=np.float32)
        b2 = np.array([[0.4]], dtype=np.float32)
        expected = ((0.2 - 0.4) / (0.2 + 0.4 + 0.5)) * 1.5
        out = compute_spectral_index(b1, b2, None, "savi")
        assert np.isclose(out[0, 0], expected, atol=1e-6)

    def test_evi_requires_third_band(self):
        b1 = np.ones((1, 1), dtype=np.float32)
        b2 = np.ones((1, 1), dtype=np.float32)
        with pytest.raises(ValueError):
            compute_spectral_index(b1, b2, None, "evi")

    def test_evi_value(self):
        b1 = np.array([[0.3]], dtype=np.float32)
        b2 = np.array([[0.2]], dtype=np.float32)
        b3 = np.array([[0.1]], dtype=np.float32)
        denom = b1 + 6.0 * b2 - 7.5 * b3 + 1.0
        expected = 2.5 * (b1 - b2) / denom
        out = compute_spectral_index(b1, b2, b3, "evi")
        assert np.isclose(out[0, 0], expected[0, 0], atol=1e-6)

    def test_bsi_requires_third_band(self):
        with pytest.raises(ValueError):
            compute_spectral_index(np.ones((1, 1)), np.ones((1, 1)), None, "bsi")

    def test_fvc_bounds(self):
        b1 = np.array([[0.0], [0.8]], dtype=np.float32)
        b2 = np.array([[0.2], [0.2]], dtype=np.float32)
        out = compute_spectral_index(b1, b2, None, "fvc")
        assert out.min() >= 0.0 and out.max() <= 1.0

    def test_case_insensitive(self):
        b1 = np.array([[0.1]], dtype=np.float32)
        b2 = np.array([[0.3]], dtype=np.float32)
        lo = compute_spectral_index(b1, b2, None, "ndvi")
        up = compute_spectral_index(b1, b2, None, "NDVI")
        assert np.isclose(lo, up).all()


class TestConvolve3x3:
    def test_identity_kernel_keeps_interior(self):
        arr = np.arange(16, dtype=np.float32).reshape(4, 4)
        kernel = np.array([[0, 0, 0], [0, 1, 0], [0, 0, 0]], dtype=np.float32)
        out = convolve3x3(arr, kernel)
        assert out[1, 1] == arr[1, 1]
        assert out[0, 0] == 0  # border untouched

    def test_uniform_kernel_averages(self):
        arr = np.full((4, 4), 8.0, dtype=np.float32)
        kernel = np.full((3, 3), 1.0 / 9.0, dtype=np.float32)
        out = convolve3x3(arr, kernel)
        assert np.isclose(out[1, 1], 8.0)

    def test_sobel_gradient(self):
        # Horizontal ramp -> strong x-gradient magnitude
        ramp = np.arange(16, dtype=np.float32).reshape(4, 4)
        kx = np.array([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]], dtype=np.float32)
        out = convolve3x3(ramp, kx)
        assert out[2, 2] > 0


class TestKMeans:
    def test_separates_two_blobs(self):
        rng = np.random.RandomState(0)
        a = rng.normal(0, 0.5, (50, 2))
        b = rng.normal(10, 0.5, (50, 2))
        X = np.vstack([a, b])
        labels = kmeans_labels(X, k=2, max_iters=20, seed=0)
        assert set(labels.tolist()) == {0, 1}
        # Blobs must be separated, not mixed
        assert (labels[:50] == labels[:50][0]).all()

    def test_deterministic_seed(self):
        rng = np.random.RandomState(1)
        X = rng.normal(0, 1, (30, 3))
        l1 = kmeans_labels(X, k=3, seed=7)
        l2 = kmeans_labels(X, k=3, seed=7)
        assert (l1 == l2).all()


class TestAreaStatistics:
    def test_percent_and_area(self):
        arr = np.array([
            [1, 1, 2],
            [1, 0, 2],
            [0, 0, 2],
        ])
        results = compute_area_statistics(arr, pixel_area=100.0)
        by_id = {r["class_id"]: r for r in results}
        assert set(by_id) == {1, 2}
        assert by_id[1]["pixels"] == 3
        assert by_id[1]["area_m2"] == 300.0
        assert by_id[2]["pixels"] == 3
        assert by_id[2]["area_m2"] == 300.0
        assert np.isclose(by_id[1]["percent"], 50.0)
        assert np.isclose(by_id[2]["percent"], 50.0)

    def test_mu_conversion(self):
        arr = np.array([[1]])
        results = compute_area_statistics(arr, pixel_area=666.6667)
        assert np.isclose(results[0]["area_mu"], 1.0, atol=1e-3)

    def test_empty_returns_empty_list(self):
        arr = np.zeros((3, 3))
        assert compute_area_statistics(arr, 1.0) == []
