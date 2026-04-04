import Mathlib.Tactic
import Mathlib.Data.Nat.Prime.Basic

/-!
# Benchmark: Number Theory

Number-theoretic theorems for benchmarking.
-/

/-- 2 is prime. -/
theorem two_is_prime : Nat.Prime 2 := Nat.prime_iff.mpr ⟨by norm_num, by omega⟩

/-- There are infinitely many primes (mathlib version). -/
theorem infinitely_many_primes : ∀ n : ℕ, ∃ p, n ≤ p ∧ Nat.Prime p :=
  Nat.exists_infinite_primes

/-- TODO: Fermat's little theorem benchmark. -/
-- theorem fermat_little (p : ℕ) (hp : Nat.Prime p) (a : ℕ) (ha : ¬ p ∣ a) :
--     a ^ (p - 1) ≡ 1 [MOD p] := by
--   sorry
