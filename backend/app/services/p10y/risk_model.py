"""
Risk model for evaluating estimation quality and calculating buffers.

This module implements a risk-based estimation algorithm that:
1. Performs gatekeeping (accept/reject) based on estimation stability
2. Calculates appropriate buffers based on variance and project size
3. Produces final estimates with risk-adjusted pricing
"""

import math
import statistics
from typing import Dict, List


class RiskModel:
    """
    Risk model for evaluating and buffering multi-workspace estimations.
    
    The model performs two main functions:
    - Gatekeeping: Determines if estimates are stable enough to use
    - Buffering: Calculates appropriate risk buffers based on variance and size
    """
    
    def __init__(
        self,
        # Buffer parameters
        k_base: float = 0.10,
        k_var: float = 1.2,
        k_size: float = 0.15,
        size_midpoint: float = 100,
        size_steepness: float = 0.05,
        # Gatekeeping parameters
        tol_small: float = 0.30,
        tol_large: float = 0.10,
        limit_small: float = 60,
        limit_large: float = 300,
    ):
        """
        Initialize RiskModel with configurable parameters.
        
        Args:
            k_base: Base buffer percentage (default 10%)
            k_var: Variance penalty multiplier (default 1.2)
            k_size: Size penalty coefficient (default 15%)
            size_midpoint: Midpoint for sigmoid size penalty (default 100h)
            size_steepness: Steepness of sigmoid curve (default 0.05)
            tol_small: Tolerance threshold for small projects (default 30%)
            tol_large: Tolerance threshold for large projects (default 10%)
            limit_small: Size threshold for small projects (default 60h)
            limit_large: Size threshold for large projects (default 300h)
        """
        # Buffer parameters
        self.k_base = k_base
        self.k_var = k_var
        self.k_size = k_size
        self.size_midpoint = size_midpoint
        self.size_steepness = size_steepness
        
        # Gatekeeping parameters
        self.tol_small = tol_small
        self.tol_large = tol_large
        self.limit_small = limit_small
        self.limit_large = limit_large

    def sigmoid_penalty(self, hours: float) -> float:
        """
        Calculate the size penalty using a logistic (sigmoid) function.
        
        Larger projects get a higher penalty to account for increased complexity
        and risk. The sigmoid function provides a smooth transition.
        
        Args:
            hours: Mean estimated hours
            
        Returns:
            Size penalty as a decimal (e.g., 0.15 = 15%)
        """
        return self.k_size / (1 + math.exp(-self.size_steepness * (hours - self.size_midpoint)))

    def get_max_tolerance(self, mean_hours: float) -> float:
        """
        Calculate the dynamic threshold based on project size using linear interpolation.
        
        Smaller projects are allowed more variance (30%), while larger projects
        require tighter consistency (10%). The threshold slides linearly between
        these values based on project size.
        
        Args:
            mean_hours: Mean estimated hours across workspaces
            
        Returns:
            Maximum allowed instability ratio (e.g., 0.30 = 30%)
        """
        if mean_hours <= self.limit_small:
            return self.tol_small
        elif mean_hours >= self.limit_large:
            return self.tol_large
        else:
            # Linear interpolation between small and large thresholds
            progress = (mean_hours - self.limit_small) / (self.limit_large - self.limit_small)
            return self.tol_small - (progress * (self.tol_small - self.tol_large))

    def evaluate(self, estimates: List[float]) -> Dict:
        """
        Run the full pipeline: Gatekeeping -> Buffering -> Pricing.
        
        This is the main entry point that:
        1. Calculates statistics from estimates
        2. Performs gatekeeping (accept/reject decision)
        3. Calculates risk-adjusted buffer
        4. Produces final estimate
        
        Args:
            estimates: List of hour estimates from different workspaces
            
        Returns:
            Dictionary containing:
            - inputs: Original estimates
            - mean: Mean of estimates
            - sigma: Standard deviation
            - cv: Coefficient of variation
            - instability_ratio: sigma / minimum (gatekeeping metric)
            - rejection_threshold: Maximum allowed instability
            - status: "Approved" or "Rejected"
            - base_component: Base buffer percentage
            - var_component: Variance penalty component
            - size_component: Size penalty component
            - total_buffer_pct: Total buffer as percentage
            - final_estimate: Final buffered estimate in hours
        """
        if not estimates:
            raise ValueError("No estimates provided")
        
        # Filter out invalid estimates
        valid_estimates = [e for e in estimates if e and float(e) > 0]
        if not valid_estimates:
            raise ValueError("No valid estimates provided")
        
        mu = statistics.mean(valid_estimates)
        minimum = min(valid_estimates)
        
        if len(valid_estimates) > 1:
            sigma = statistics.stdev(valid_estimates)
        else:
            sigma = 0

        # --- 1. GATEKEEPING (Decision Logic) ---
        # Instability Ratio: How 'messy' is the spread relative to the optimistic case?
        instability_ratio = sigma / minimum if minimum > 0 else 0
        
        # Dynamic Tolerance: How much mess do we allow for a project of this size?
        rejection_threshold = self.get_max_tolerance(mu)
        
        # Decision: Approved if instability is within threshold
        status = "Approved" if instability_ratio <= rejection_threshold else "Rejected"

        # --- 2. BUFFERING (Pricing Logic) ---
        # Even if rejected, we calculate the numbers for the report
        cv = sigma / mu if mu > 0 else 0
        
        base = self.k_base
        var_penalty = cv * self.k_var
        size_penalty = self.sigmoid_penalty(mu)
        
        total_buffer_pct = base + var_penalty + size_penalty
        final_hours = mu * (1 + total_buffer_pct)

        return {
            "inputs": valid_estimates,
            "mean": mu,
            "sigma": sigma,
            "cv": cv,
            
            # Gatekeeping Metrics
            "instability_ratio": instability_ratio,
            "rejection_threshold": rejection_threshold,
            "status": status,
            
            # Buffer Metrics
            "base_component": base,
            "var_component": var_penalty,
            "size_component": size_penalty,
            "total_buffer_pct": total_buffer_pct,
            "final_estimate": final_hours
        }
    
    # Alias for compatibility with old code if needed
    def estimate(self, estimates: List[float]) -> Dict:
        """Alias for evaluate() for backward compatibility."""
        return self.evaluate(estimates)
