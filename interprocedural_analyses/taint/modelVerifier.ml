(* Copyright (c) 2016-present, Facebook, Inc.
 *
 * This source code is licensed under the MIT license found in the
 * LICENSE file in the root directory of this source tree. *)

open Core
open Pyre
open Ast
open Analysis
open Expression

let raise_invalid_model message = raise (Model.InvalidModel message)

type parameter_requirements = {
  required_anonymous_parameters_count: int;
  optional_anonymous_parameters_count: int;
  required_parameter_set: String.Set.t;
  optional_parameter_set: String.Set.t;
  has_star_parameter: bool;
  has_star_star_parameter: bool;
}

let create_parameters_requirements ~type_parameters =
  let get_parameters_requirements requirements type_parameter =
    let open Type.Callable.RecordParameter in
    match type_parameter with
    | PositionalOnly { default; _ } ->
        if default then
          {
            requirements with
            optional_anonymous_parameters_count =
              requirements.optional_anonymous_parameters_count + 1;
          }
        else
          {
            requirements with
            required_anonymous_parameters_count =
              requirements.required_anonymous_parameters_count + 1;
          }
    | Named { name; default; _ }
    | KeywordOnly { name; default; _ } ->
        let name = Identifier.sanitized name in
        if default then
          {
            requirements with
            optional_parameter_set = String.Set.add requirements.optional_parameter_set name;
          }
        else
          {
            requirements with
            required_parameter_set = String.Set.add requirements.required_parameter_set name;
          }
    | Variable _ -> { requirements with has_star_parameter = true }
    | Keywords _ -> { requirements with has_star_star_parameter = true }
  in
  let init =
    {
      required_anonymous_parameters_count = 0;
      optional_anonymous_parameters_count = 0;
      required_parameter_set = String.Set.empty;
      optional_parameter_set = String.Set.empty;
      has_star_parameter = false;
      has_star_star_parameter = false;
    }
  in
  List.fold_left type_parameters ~f:get_parameters_requirements ~init


let demangle_class_attribute name =
  if String.is_substring ~substring:"__class__" name then
    String.split name ~on:'.'
    |> List.rev
    |> function
    | attribute :: "__class__" :: rest -> List.rev (attribute :: rest) |> String.concat ~sep:"."
    | _ -> name
  else
    name


let model_compatible ~type_parameters ~normalized_model_parameters =
  let parameter_requirements = create_parameters_requirements ~type_parameters in
  (* Once a requirement has been satisfied, it is removed from requirement object. At the end, we
     check whether there remains unsatisfied requirements. *)
  let validate_model_parameter (errors, requirements) (model_parameter, _, original) =
    (* Ensure that the parameter's default value is either not present or `...` to catch common
       errors when declaring models. *)
    let () =
      match Node.value original with
      | { Parameter.value = Some expression; name; _ } ->
          if not (Expression.equal_expression (Node.value expression) Expression.Ellipsis) then
            let message =
              Format.sprintf
                "Default values of parameters must be `...`. Did you mean to write `%s: %s`?"
                name
                (Expression.show expression)
            in
            raise_invalid_model message
      | _ -> ()
    in
    let open AccessPath.Root in
    match model_parameter with
    | LocalResult
    | Variable _ ->
        failwith
          ( "LocalResult|Variable won't be generated by AccessPath.Root.normalize_parameters, "
          ^ "and they cannot be compared with type_parameters." )
    | PositionalParameter { name; positional_only = true; _ } ->
        let { required_anonymous_parameters_count; _ } = requirements in
        if required_anonymous_parameters_count >= 1 then
          ( errors,
            {
              requirements with
              required_anonymous_parameters_count = required_anonymous_parameters_count - 1;
            } )
        else
          Format.sprintf "unexpected positional only parameter: `%s`" name :: errors, requirements
    | PositionalParameter { name; _ }
    | NamedParameter { name } ->
        let name = Identifier.sanitized name in
        if String.is_prefix name ~prefix:"__" then (* It is an positional only parameter. *)
          let {
            required_anonymous_parameters_count;
            optional_anonymous_parameters_count;
            has_star_parameter;
            _;
          }
            =
            requirements
          in
          if required_anonymous_parameters_count >= 1 then
            ( errors,
              {
                requirements with
                required_anonymous_parameters_count = required_anonymous_parameters_count - 1;
              } )
          else if optional_anonymous_parameters_count >= 1 then
            ( errors,
              {
                requirements with
                optional_anonymous_parameters_count = optional_anonymous_parameters_count - 1;
              } )
          else if has_star_parameter then
            (* If all positional only parameter quota is used, it might be covered by a `*args` *)
            errors, requirements
          else
            Format.sprintf "unexpected positional only parameter: `%s`" name :: errors, requirements
        else
          let {
            required_parameter_set;
            optional_parameter_set;
            has_star_parameter;
            has_star_star_parameter;
            _;
          }
            =
            requirements
          in
          (* Consume an required or optional named parameter. *)
          if String.Set.mem required_parameter_set name then
            let required_parameter_set = String.Set.remove required_parameter_set name in
            errors, { requirements with required_parameter_set }
          else if String.Set.mem optional_parameter_set name then
            let optional_parameter_set = String.Set.remove optional_parameter_set name in
            errors, { requirements with optional_parameter_set }
          else if has_star_parameter || has_star_star_parameter then
            (* If the name is not found in the set, it might be covered by ``**kwargs` *)
            errors, requirements
          else
            Format.sprintf "unexpected named parameter: `%s`" name :: errors, requirements
    | StarParameter _ ->
        if requirements.has_star_parameter then
          errors, requirements
        else
          "unexpected star parameter" :: errors, requirements
    | StarStarParameter _ ->
        if requirements.has_star_star_parameter then
          errors, requirements
        else
          "unexpected star star parameter" :: errors, requirements
  in
  let errors, left_over =
    List.fold_left
      normalized_model_parameters
      ~f:validate_model_parameter
      ~init:([], parameter_requirements)
  in
  let { required_anonymous_parameters_count; required_parameter_set; _ } = left_over in
  let errors =
    if required_anonymous_parameters_count > 0 then
      Format.sprintf "missing %d positional only parameters" required_anonymous_parameters_count
      :: errors
    else
      errors
  in
  let errors =
    if String.Set.is_empty required_parameter_set then
      errors
    else
      Format.sprintf
        "missing named parameters: `%s`"
        (required_parameter_set |> String.Set.to_list |> String.concat ~sep:", ")
      :: errors
  in
  errors


let verify_signature ~normalized_model_parameters ~name callable_annotation =
  match callable_annotation with
  | Some
      ( {
          Type.Callable.implementation =
            { Type.Callable.parameters = Type.Callable.Defined implementation_parameters; _ };
          kind;
          _;
        } as callable ) -> (
      let error =
        match kind with
        | Type.Callable.Named actual_name when not (Reference.equal name actual_name) ->
            Some
              (Format.asprintf
                 "The modelled function is an imported function `%a`, please model it directly."
                 Reference.pp
                 actual_name)
        | _ ->
            let model_compatibility_errors =
              model_compatible
                ~type_parameters:implementation_parameters
                ~normalized_model_parameters
            in
            if List.is_empty model_compatibility_errors then
              None
            else
              Some
                (Format.asprintf
                   "Model signature parameters do not match implementation `%s`. Reason(s): %s."
                   (Type.show_for_hover (Type.Callable callable))
                   (String.concat model_compatibility_errors ~sep:"; "))
      in
      match error with
      | Some error ->
          Log.error "%s" error;
          raise_invalid_model error
      | None -> () )
  | _ -> ()
