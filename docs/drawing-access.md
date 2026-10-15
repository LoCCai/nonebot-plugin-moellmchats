# Drawing authorization

Hosts can register an async `ToolContext -> bool` callback with
`media_tools.register_image_generation_authorizer`. Drawing and image editing
check this callback before reading references, and again after waiting for a
drawing slot, immediately before calling the image provider. Only the literal
result `True` allows generation. Errors and timeouts deny access; cancellation
continues to propagate. The callback receives the trusted caller context.

Set `MOELLM_IMAGE_GENERATION_RESTRICTED=true` in the host's NoneBot configuration
to deny generation if the callback has not been installed. The standalone
default is `false` for compatibility. A registered callback always takes
precedence over that default. Direct command integrations should call
`await can_generate_image(context)` before collecting message attachments;
`generate_image` also enforces the check itself.

Qiqi registers its QWeb authorization callback at startup. Only explicit manual
grants to the account's current QQ allow drawing. Group roles, paid membership,
and administrator status do not automatically grant use. QWeb administrators
can grant and revoke access in the accounts and authorization page, with CSRF
protection and an audit record. QQ changes and account deactivation invalidate
the existing grant. This policy covers AI image generation and editing.
