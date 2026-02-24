#!/usr/bin/env python3
"""
XBai-04 Data Explorer
Explore the mathematical reasoning datasets
"""

import pandas as pd
import json
import sys
import os

def explore_training_data():
    """Explore the training dataset"""
    print("=== TRAINING DATA EXPLORATION ===")
    df = pd.read_parquet('data/train.parquet')
    
    print(f"Dataset size: {len(df)} problems")
    print(f"Columns: {df.columns.tolist()}")
    print("\nData distribution:")
    print(df['data_source'].value_counts())
    
    # Sample a few problems
    print("\n=== SAMPLE PROBLEMS ===")
    for i in range(3):
        print(f"\nProblem {i+1}:")
        print(f"Q: {df.iloc[i]['question'][:200]}...")
        print(f"A: {df.iloc[i]['solution'][:100]}...")

def explore_test_data():
    """Explore the test datasets"""
    print("\n=== TEST DATA EXPLORATION ===")
    
    # AIME 2024
    with open('test/data/aime24.jsonl', 'r') as f:
        aime24 = [json.loads(line) for line in f]
    
    print(f"AIME 2024: {len(aime24)} problems")
    print(f"Sample answer: {aime24[0]['answer']}")
    
    # AIME 2025  
    with open('test/data/aime25.jsonl', 'r') as f:
        aime25 = [json.loads(line) for line in f]
        
    print(f"AIME 2025: {len(aime25)} problems")

def test_math_evaluation():
    """Test the mathematical evaluation system"""
    print("\n=== MATH EVALUATION TESTING ===")
    
    sys.path.append('test')
    from prime_math import math_normalize, grader
    
    test_cases = [
        ("204", "204"),
        ("113/1", "113"),
        ("\\frac{113}{1}", "113"),
        ("371", "371")
    ]
    
    print("Testing answer normalization:")
    for answer, expected in test_cases:
        normalized = math_normalize.normalize_answer(answer)
        is_correct = grader.grade_answer(normalized, expected)
        print(f"  {answer} -> {normalized} (correct: {is_correct})")

if __name__ == "__main__":
    # Change to project directory
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    
    explore_training_data()
    explore_test_data() 
    test_math_evaluation()
    
    print("\n=== NEXT STEPS ===")
    print("1. Analyze problem difficulty patterns")
    print("2. Study solution formats")
    print("3. Test evaluation on sample problems")
    print("4. Consider cloud-based training for full pipeline")
