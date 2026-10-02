"use strict";

const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

const {buildTaskTypeOptions} = require(
  path.join(__dirname, "..", "..", "src", "lattice", "dashboard", "static", "task-type-options.js")
);

test("configured type keeps its position and remains enabled", () => {
  assert.deepStrictEqual(
    buildTaskTypeOptions(["task", "bug", "chore"], "bug", true),
    [
      {value: "task", selected: false, disabled: false},
      {value: "bug", selected: true, disabled: false},
      {value: "chore", selected: false, disabled: false}
    ]
  );
});

test("unconfigured stored type is prepended, selected, and disabled only in edit selects", () => {
  assert.deepStrictEqual(
    buildTaskTypeOptions(["task", "bug", "chore"], "spike", true),
    [
      {value: "spike", selected: true, disabled: true},
      {value: "task", selected: false, disabled: false},
      {value: "bug", selected: false, disabled: false},
      {value: "chore", selected: false, disabled: false}
    ]
  );
  assert.deepStrictEqual(
    buildTaskTypeOptions(["task", "bug", "chore"], "spike", false).map((option) => option.value),
    ["task", "bug", "chore"]
  );
});

test("create options stay within the configured list", () => {
  assert.deepStrictEqual(
    buildTaskTypeOptions(["task", "research"], "task", false),
    [
      {value: "task", selected: true, disabled: false},
      {value: "research", selected: false, disabled: false}
    ]
  );
});
