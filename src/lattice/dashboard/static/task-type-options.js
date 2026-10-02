"use strict";

// Pure task-type option builder for the dashboard's create and edit selects.
// A legacy stored value may no longer be configured, but must remain visible
// in an edit select without becoming an option for new tasks.
function buildTaskTypeOptions(configuredTypes, selectedType, preserveUnconfigured) {
  var types = Array.isArray(configuredTypes) ? configuredTypes : [];
  var options = types.map(function(type) {
    return {
      value: type,
      selected: type === selectedType,
      disabled: false
    };
  });

  if (preserveUnconfigured && typeof selectedType === "string"
      && selectedType && types.indexOf(selectedType) === -1) {
    options.unshift({value: selectedType, selected: true, disabled: true});
  }
  return options;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {buildTaskTypeOptions};
}
