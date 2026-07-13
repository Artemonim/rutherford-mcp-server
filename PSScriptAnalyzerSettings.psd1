@{
    Severity = @(
        "Error",
        "Warning"
    )

    IncludeRules = @("*")

    ExcludeRules = @(
        "PSAvoidUsingWriteHost",
        "PSUseShouldProcessForStateChangingFunctions"
    )
}
