<#import "template.ftl" as layout>
<@layout.registrationLayout displayMessage=false; section>
    <#if section = "header">
        <link href="${url.resourcesPath}/img/favicon.png" rel="icon"/>
    <#elseif section = "form">
        <div class="card card-reset">
            <div class="logo" style="text-align: center; margin-bottom: 24px;">
                <img src="${url.resourcesPath}/img/ttc.logo2.svg" alt="TTC Logo">
            </div>

            <div class="info-icon" style="text-align: center; margin-bottom: 20px;">
                <span style="display: inline-flex; width: 64px; height: 64px;
                             border-radius: 50%; background: #f0fdf4;
                             align-items: center; justify-content: center;
                             color: #10b981; font-size: 36px; font-weight: 700;
                             line-height: 1;">&check;</span>
            </div>

            <div style="text-align: center; margin-bottom: 30px;">
                <h2 class="title" style="font-size: 24px;">
                    ${message.summary}
                    <#if requiredActions??>
                        <#list requiredActions>: <#items as reqAction>${kcSanitize(msg("requiredAction.${reqAction}"))?no_esc}<#sep>, </#sep></#items></#list>
                    </#if>
                </h2>
            </div>

            <#if !skipLink??>
                <#if pageRedirectUri?has_content>
                <a href="${pageRedirectUri}" class="submit-btn"
                   style="display: block; text-align: center; text-decoration: none;">
                    ${kcSanitize(msg("backToApplication"))?no_esc}
                </a>
                <#elseif actionUri?has_content>
                <a href="${actionUri}" class="submit-btn"
                   style="display: block; text-align: center; text-decoration: none;">
                    ${kcSanitize(msg("proceedWithAction"))?no_esc}
                </a>
                <#elseif client?? && client.baseUrl?has_content>
                <a href="${client.baseUrl}" class="submit-btn"
                   style="display: block; text-align: center; text-decoration: none;">
                    ${kcSanitize(msg("backToApplication"))?no_esc}
                </a>
                </#if>
            </#if>
        </div>
    </#if>
</@layout.registrationLayout>
