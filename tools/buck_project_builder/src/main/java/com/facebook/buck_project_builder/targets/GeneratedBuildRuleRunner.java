package com.facebook.buck_project_builder.targets;

import com.google.common.collect.ImmutableList;

import javax.annotation.Nullable;
import java.io.BufferedReader;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.file.Paths;
import java.util.stream.Collectors;

final class GeneratedBuildRuleRunner {

  private GeneratedBuildRuleRunner() {}

  static String getBasePathPrefixedSources(
      @Nullable String cellPath, String basePath, ImmutableList<String> sources) {
    String cellAndBasePathPrefix =
        Paths.get(cellPath != null ? cellPath : ".", basePath).toString();
    return sources.stream()
        .map(source -> Paths.get(cellAndBasePathPrefix, source).toString())
        .collect(Collectors.joining(" "));
  }

  static void runBuilderCommand(String builderCommand, String buckRoot) throws IOException {
    try (InputStream errorStream =
        // Run the command in replaced cmd directly.
        Runtime.getRuntime()
            .exec(
                builderCommand,
                /* environment variables */ null,
                /* working directory */ new File(buckRoot))
            .getErrorStream()) {
      new BufferedReader(new InputStreamReader(errorStream)).lines().forEach(System.err::println);
    }
  }
}
