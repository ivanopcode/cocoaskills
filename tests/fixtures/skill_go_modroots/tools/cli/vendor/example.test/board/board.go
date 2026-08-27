package board

// Label is first-party source of the same package as the build root. Under
// -mod=vendor the compiler reads the vendor copy of this module, and the
// manager validates this directory because the manifest declared it.
func Label() string { return "module-root" }
