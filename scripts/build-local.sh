#!/bin/bash

# we just want to package the custom_components/sems_plus directory to the tmp/ folder

OUTPUT_FILE="tmp/sems_plus.tar"

mkdir -p tmp
rm -f "$OUTPUT_FILE"

# tar just the contents of custom_components/sems_plus into the output file
tar -cvf "$OUTPUT_FILE" -C custom_components/sems_plus .
