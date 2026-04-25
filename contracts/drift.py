def compute_drift(total_calls: int, hard: int, soft: int, recovered: int) -> dict:
    """
    Compute behavioral drift metrics (Drift Bounds Theorem from Agent Behavioral Contracts paper).

    alpha = (hard + soft) / total_calls   # violation rate
    gamma = recovered / (hard + soft)     # recovery rate (1.0 if no violations)
    drift = alpha / gamma                  # D* bounded drift score
    """
    violations = hard + soft
    alpha = violations / total_calls if total_calls > 0 else 0.0
    gamma = recovered / violations if violations > 0 else 1.0
    drift = alpha / gamma if gamma > 0 else alpha
    return {
        "alpha": round(alpha, 4),
        "gamma": round(gamma, 4),
        "drift": round(drift, 4),
        "violations": violations,
    }
