#!/usr/bin/python
#
# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Generates jax_releases.html package index.

To update public copy, use:
gsutil cp jax_releases.html gs://jax-releases/
"""

import subprocess

FILENAME = "jax_releases.html"

HEADER = """
<!-- Generated by jax/build/generate_release_index.py, do not update manually! -->
<html>
<head><meta http-equiv="Content-Type" content="text/html; charset=utf-8"></head>
<body>
"""

FOOTER = "</body>\n</html>\n"

print("Running command: gsutil ls gs://jax-releases/cuda*")
ls_output = subprocess.check_output(["gsutil", "ls", "gs://jax-releases/cuda*"])

print(f"Writing index to {FILENAME}")
with open(FILENAME, "w") as f:
  f.write(HEADER)
  for line in ls_output.decode("utf-8").split("\n"):
    # Skip incorrectly formatted wheel filenames and other gsutil output
    if not "+cuda" in line: continue
    # Example line:
    # gs://jax-releases/cuda101/jaxlib-0.1.52+cuda101-cp38-none-manylinux2010_x86_64.whl
    assert line.startswith("gs://jax-releases/cuda")
    link_title = line[len("gs://jax-releases/"):]
    link_href = line.replace("gs://", "https://storage.googleapis.com/")
    f.write(f'<a href="{link_href}">{link_title}</a><br>\n')
  f.write(FOOTER)
print("Done.")
