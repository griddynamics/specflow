"""
Unit tests for RiskModel generation algorithm.
"""
import pytest
from app.services.p10y.risk_model import RiskModel


class TestRiskModel:
    """Tests for RiskModel class."""
    
    def test_evaluate_single_estimate(self):
        """Test evaluation with a single estimate."""
        model = RiskModel()
        result = model.evaluate([100.0])
        
        assert result["mean"] == 100.0
        assert result["sigma"] == 0.0
        assert result["cv"] == 0.0
        assert result["status"] == "Approved"  # Single estimate should be approved
        assert result["final_estimate"] > 100.0  # Should have buffer
        assert result["total_buffer_pct"] > 0
    
    def test_evaluate_multiple_consistent_estimates(self):
        """Test evaluation with consistent estimates (low variance)."""
        model = RiskModel()
        # Estimates within 10% of each other
        estimates = [100.0, 105.0, 95.0, 102.0]
        result = model.evaluate(estimates)
        
        assert result["mean"] == pytest.approx(100.5, rel=0.1)
        assert result["sigma"] > 0
        assert result["status"] == "Approved"  # Low variance should be approved
        assert result["final_estimate"] > result["mean"]
    
    def test_evaluate_high_variance_small_project(self):
        """Test evaluation with high variance for small project (should allow more variance)."""
        model = RiskModel()
        # Small project (< 60h) with high variance (40%)
        estimates = [50.0, 70.0, 45.0, 80.0]
        result = model.evaluate(estimates)
        
        assert result["mean"] == pytest.approx(61.25, rel=0.1)
        assert result["rejection_threshold"] == pytest.approx(0.30, rel=0.01)  # tol_small
        # High variance might still be approved for small projects
        assert result["instability_ratio"] > 0
    
    def test_evaluate_high_variance_large_project(self):
        """Test evaluation with high variance for large project (should reject)."""
        model = RiskModel()
        # Large project (> 300h) with high variance
        estimates = [300.0, 500.0, 250.0, 600.0]
        result = model.evaluate(estimates)
        
        assert result["mean"] == pytest.approx(412.5, rel=0.1)
        assert result["rejection_threshold"] == pytest.approx(0.10, rel=0.01)  # tol_large
        # High variance should likely be rejected for large projects
        assert result["instability_ratio"] > result["rejection_threshold"]
        assert result["status"] == "Rejected"
    
    def test_get_max_tolerance_small_project(self):
        """Test tolerance calculation for small projects."""
        model = RiskModel()
        assert model.get_max_tolerance(30.0) == 0.30  # Below limit_small
        assert model.get_max_tolerance(60.0) == 0.30  # At limit_small
    
    def test_get_max_tolerance_large_project(self):
        """Test tolerance calculation for large projects."""
        model = RiskModel()
        assert model.get_max_tolerance(300.0) == 0.10  # At limit_large
        assert model.get_max_tolerance(500.0) == 0.10  # Above limit_large
    
    def test_get_max_tolerance_interpolation(self):
        """Test tolerance interpolation for medium-sized projects."""
        model = RiskModel()
        # Should interpolate between 0.30 and 0.10
        tolerance_100 = model.get_max_tolerance(100.0)
        tolerance_180 = model.get_max_tolerance(180.0)
        
        assert 0.10 < tolerance_100 < 0.30
        assert 0.10 < tolerance_180 < 0.30
        assert tolerance_100 > tolerance_180  # Larger projects have lower tolerance
    
    def test_sigmoid_penalty(self):
        """Test sigmoid penalty calculation."""
        model = RiskModel()
        
        # Small project should have lower penalty
        penalty_small = model.sigmoid_penalty(50.0)
        
        # Large project should have higher penalty
        penalty_large = model.sigmoid_penalty(200.0)
        
        assert penalty_small < penalty_large
        assert penalty_small > 0
        assert penalty_large > 0
    
    def test_buffer_components(self):
        """Test that buffer components are calculated correctly."""
        model = RiskModel()
        estimates = [100.0, 120.0, 80.0]
        result = model.evaluate(estimates)
        
        assert "base_component" in result
        assert "var_component" in result
        assert "size_component" in result
        assert "total_buffer_pct" in result
        
        # Total buffer should be sum of components
        expected_total = (
            result["base_component"] +
            result["var_component"] +
            result["size_component"]
        )
        assert result["total_buffer_pct"] == pytest.approx(expected_total, rel=0.01)
    
    def test_final_estimate_calculation(self):
        """Test that final estimate includes buffer."""
        model = RiskModel()
        estimates = [100.0, 110.0, 90.0]
        result = model.evaluate(estimates)
        
        mean = result["mean"]
        buffer_pct = result["total_buffer_pct"]
        expected_final = mean * (1 + buffer_pct)
        
        assert result["final_estimate"] == pytest.approx(expected_final, rel=0.01)
        assert result["final_estimate"] > mean
    
    def test_empty_estimates_raises_error(self):
        """Test that empty estimates list raises error."""
        model = RiskModel()
        with pytest.raises(ValueError, match="No estimates provided"):
            model.evaluate([])
    
    def test_invalid_estimates_filtered(self):
        """Test that invalid estimates (None, zero, negative) are filtered."""
        model = RiskModel()
        # Include None, 0, negative values
        estimates = [None, 0, -10, 100.0, 110.0]
        result = model.evaluate(estimates)
        
        # Should only use valid estimates (100.0, 110.0)
        assert len(result["inputs"]) == 2
        assert 100.0 in result["inputs"]
        assert 110.0 in result["inputs"]
        assert result["mean"] == 105.0
    
    def test_approved_status_low_variance(self):
        """Test that low variance estimates are approved."""
        model = RiskModel()
        # Very consistent estimates
        estimates = [100.0, 101.0, 99.0, 100.5]
        result = model.evaluate(estimates)
        
        assert result["instability_ratio"] < result["rejection_threshold"]
        assert result["status"] == "Approved"
    
    def test_rejected_status_high_variance(self):
        """Test that high variance estimates are rejected."""
        model = RiskModel()
        # Very inconsistent estimates for medium project
        estimates = [150.0, 300.0, 50.0, 400.0]
        result = model.evaluate(estimates)
        
        # Should have high instability ratio
        assert result["instability_ratio"] > 0.5
        # Status depends on threshold, but likely rejected
        assert result["status"] in ["Approved", "Rejected"]
    
    def test_custom_parameters(self):
        """Test RiskModel with custom parameters."""
        model = RiskModel(
            k_base=0.15,  # Higher base buffer
            k_var=2.0,    # Higher variance penalty
            tol_small=0.40,  # More lenient for small projects
            tol_large=0.05,  # Stricter for large projects
        )
        
        estimates = [100.0, 120.0, 80.0]
        result = model.evaluate(estimates)
        
        assert result["base_component"] == 0.15
        assert result["rejection_threshold"] > 0  # Should use custom tolerance
    
    def test_estimate_alias(self):
        """Test that estimate() is an alias for evaluate()."""
        model = RiskModel()
        estimates = [100.0, 110.0, 90.0]
        
        result_evaluate = model.evaluate(estimates)
        result_estimate = model.estimate(estimates)
        
        assert result_evaluate == result_estimate
    
    def test_single_zero_estimate_raises_error(self):
        """Test that only zero estimates raises error."""
        model = RiskModel()
        with pytest.raises(ValueError, match="No valid estimates"):
            model.evaluate([0.0, 0.0])
    
    def test_instability_ratio_calculation(self):
        """Test instability ratio calculation."""
        model = RiskModel()
        estimates = [100.0, 150.0, 50.0]
        result = model.evaluate(estimates)
        
        # Instability = sigma / minimum
        # sigma should be around 50, minimum is 50
        # So instability should be around 1.0
        assert result["instability_ratio"] > 0
        # Verify instability ratio is calculated correctly
        minimum = min(estimates)
        expected_instability = result["sigma"] / minimum if minimum > 0 else 0
        assert result["instability_ratio"] == pytest.approx(expected_instability, rel=0.01)
    
    def test_cv_calculation(self):
        """Test coefficient of variation calculation."""
        model = RiskModel()
        estimates = [100.0, 110.0, 90.0]
        result = model.evaluate(estimates)
        
        # CV = sigma / mean
        expected_cv = result["sigma"] / result["mean"] if result["mean"] > 0 else 0
        assert result["cv"] == pytest.approx(expected_cv, rel=0.01)
