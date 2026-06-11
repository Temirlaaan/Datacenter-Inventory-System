<#import "template.ftl" as layout>
<@layout.registrationLayout displayMessage=false; section>
    <#if section = "header">
        <link href="${url.resourcesPath}/img/favicon.png" rel="icon"/>
    <#elseif section = "form">
        <div class="card card-reset">
            <div class="logo" style="text-align: center; margin-bottom: 24px;">
                <img src="${url.resourcesPath}/img/ttc.logo2.svg" alt="TTC Logo">
            </div>

            <div style="text-align: center; margin-bottom: 30px;">
                <h2 class="title" style="font-size: 24px;">Выберите способ входа</h2>
                <p style="color: #6b7280; margin-top: 10px;">
                    Вы можете использовать любой из настроенных способов
                </p>
            </div>

            <form id="kc-select-credential-form" action="${url.loginAction}" method="post">
                <div style="display: grid; gap: 12px;">
                    <#list auth.authenticationSelections as authenticationSelection>
                    <button type="submit"
                            name="authenticationExecution"
                            value="${authenticationSelection.authExecId}"
                            class="select-auth-btn"
                            style="display: block; width: 100%; padding: 16px 20px;
                                   background: #f9fafb; border: 2px solid #e5e7eb;
                                   border-radius: 12px; cursor: pointer; text-align: left;
                                   transition: all 0.2s ease;">
                        <div style="font-weight: 600; color: #1f2937; margin-bottom: 4px;">
                            ${msg('${authenticationSelection.displayName}')}
                        </div>
                        <div style="font-size: 13px; color: #6b7280;">
                            ${msg('${authenticationSelection.helpText}')}
                        </div>
                    </button>
                    </#list>
                </div>
            </form>

            <div style="margin-top: 24px; text-align: center;">
                <a href="${url.loginRestartFlowUrl}" style="color: #6b7280; font-size: 14px;">
                    Вернуться к началу
                </a>
            </div>
        </div>
    </#if>
</@layout.registrationLayout>
