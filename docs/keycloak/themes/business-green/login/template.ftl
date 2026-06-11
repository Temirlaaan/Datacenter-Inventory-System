<#macro registrationLayout
        bodyClass=""
        displayInfo=false
        displayMessage=true
        displayRequiredFields=false>
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />
    <meta name="robots" content="noindex, nofollow">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="shortcut icon" href="${url.resourcesPath}/img/favicon.png" type="image/x-icon">

    <title><#nested "title"></title>

    <#if properties.styles?has_content>
        <#list properties.styles?split(' ') as style>
            <link href="${url.resourcesPath}/${style}" rel="stylesheet" />
        </#list>
    </#if>

    <#nested "header">
</head>
<body>
    <div class="page-wrapper">
        <div class="wrapper wrapper--w780">
            <#nested "form">
        </div>
    </div>
</body>
</html>
</#macro>
