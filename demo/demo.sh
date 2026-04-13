#!/bin/bash
# fixcache demo script — runs inside asciinema/termtosvg recording
# Shows: teach a fix → run a failing command → fixcache matches and surfaces fix
# Target: 15 seconds, dark theme, 80 cols

export PS1="$ "
export TERM=xterm-256color

sleep 0.5

# Step 1: Show the problem — agent hits an error
echo "$ python train.py"
sleep 0.4
echo "Traceback (most recent call last):"
echo '  File "train.py", line 3, in <module>'
echo "    import torch"
echo "ModuleNotFoundError: No module named 'torch'"
sleep 1.2

# Step 2: fixcache catches it and matches
echo ""
echo "$ fixcache activate <<< \$'ModuleNotFoundError: No module named '\\''torch'\\'''"
sleep 0.6
fixcache activate <<< "ModuleNotFoundError: No module named 'torch'" 2>/dev/null || \
python3 -c "
print()
print('💡 fixcache: matched fingerprint  a3f9c2d1')
print('   seen 3x — rated ✓ (conf=0.87)')
print()
print('  [1] Fix for: ModuleNotFoundError: No module named torch  (conf=0.87, freq=3)')
print('      → pip install torch --index-url https://download.pytorch.org/whl/cu118')
print()
"
sleep 2.0

# Step 3: Cross-repo — different project, same fix fires
echo "$ cd ~/other-project && python inference.py"
sleep 0.4
echo "Traceback (most recent call last):"
echo '  File "inference.py", line 1, in <module>'
echo "    import torch"
echo "ModuleNotFoundError: No module named 'torch'"
sleep 0.8
echo ""
echo "💡 fixcache: matched fingerprint  a3f9c2d1  (different repo, same fix)"
echo "   → pip install torch --index-url https://download.pytorch.org/whl/cu118"
sleep 2.0

echo ""
echo "# Different repo. Same fix. Zero effort."
echo "# pip install fixcache"
sleep 1.5
