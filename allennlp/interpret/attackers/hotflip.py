# pylint: disable=protected-access
from copy import deepcopy
from typing import List

import numpy
import torch

from allennlp.common.util import JsonDict, sanitize
from allennlp.data.fields import TextField
from allennlp.data.token_indexers import ELMoTokenCharactersIndexer, TokenCharactersIndexer
from allennlp.data.tokenizers import Token
from allennlp.interpret.attackers import utils
from allennlp.interpret.attackers.attacker import Attacker
from allennlp.modules.token_embedders import Embedding
from allennlp.nn import util
from allennlp.predictors.predictor import Predictor


@Attacker.register('hotflip')
class Hotflip(Attacker):
    """
    Runs the HotFlip style attack at the word-level https://arxiv.org/abs/1712.06751.  We use the
    first-order taylor approximation described in https://arxiv.org/abs/1903.06620, in the function
    _first_order_taylor(). Constructing this object is expensive due to the construction of the
    embedding matrix.
    """
    def __init__(self, predictor: Predictor, vocab_namespace: str = 'tokens') -> None:
        super().__init__(predictor)
        self.vocab = self.predictor._model.vocab
        self.namespace = vocab_namespace
        # Force new tokens to be alphanumeric
        self.invalid_replacement_indices = []
        for i in self.vocab._index_to_token[self.namespace]:
            if not self.vocab._index_to_token[self.namespace][i].isalnum():
                self.invalid_replacement_indices.append(i)
        self.token_embedding = None

    def initialize(self):
        """
        Call this function before running attack_from_json(). We put the call to
        ``_construct_embedding_matrix()`` in this function to prevent a large amount of compute
        being done when __init__() is called.
        """
        self.token_embedding = self._construct_embedding_matrix()

    def _construct_embedding_matrix(self):
        """
        For HotFlip, we need a word embedding matrix to search over. The below is necessary for
        models such as ELMo, character-level models, or for models that use a projection layer
        after their word embeddings.

        We run all of the tokens from the vocabulary through the TextFieldEmbedder, and save the
        final output embedding. We then group all of those output embeddings into an "embedding
        matrix".
        """
        # Gets all tokens in the vocab and their corresponding IDs
        all_tokens = self.vocab._token_to_index[self.namespace]
        all_indices = list(self.vocab._index_to_token[self.namespace].keys())
        all_inputs = {"tokens": torch.LongTensor(all_indices).unsqueeze(0)}
        for token_indexer in self.predictor._dataset_reader._token_indexers.values():
            # handle when a model uses character-level inputs, e.g., a CharCNN
            if isinstance(token_indexer, TokenCharactersIndexer):
                tokens = [Token(x) for x in all_tokens]
                max_token_length = max(len(x) for x in all_tokens)
                indexed_tokens = token_indexer.tokens_to_indices(tokens, self.vocab, "token_characters")
                padded_tokens = token_indexer.as_padded_tensor(indexed_tokens,
                                                               {"token_characters": len(tokens)},
                                                               {"num_token_characters": max_token_length})
                all_inputs['token_characters'] = torch.LongTensor(padded_tokens['token_characters']).unsqueeze(0)
            # for ELMo models
            if isinstance(token_indexer, ELMoTokenCharactersIndexer):
                elmo_tokens = []
                for token in all_tokens:
                    elmo_indexed_token = token_indexer.tokens_to_indices([Token(text=token)],
                                                                         self.vocab,
                                                                         "sentence")["sentence"]
                    elmo_tokens.append(elmo_indexed_token[0])
                all_inputs["elmo"] = torch.LongTensor(elmo_tokens).unsqueeze(0)

        embedding_layer = util.find_embedding_layer(self.predictor._model)
        if isinstance(embedding_layer, torch.nn.modules.sparse.Embedding):
            embedding_matrix = embedding_layer.weight
        else:
            # pass all tokens through the fake matrix and create an embedding out of it.
            embedding_matrix = embedding_layer(all_inputs).squeeze()

        return Embedding(num_embeddings=self.vocab.get_vocab_size(self.namespace),
                         embedding_dim=embedding_matrix.shape[1],
                         weight=embedding_matrix,
                         trainable=False)

    def attack_from_json(self,
                         inputs: JsonDict = None,
                         input_field_to_attack: str = 'tokens',
                         grad_input_field: str = 'grad_input_1',
                         ignore_tokens: List[str] = None) -> JsonDict:
        """
        Replaces one token at a time from the input until the model's prediction changes.
        ``input_field_to_attack`` is for example ``tokens``, it says what the input field is
        called.  ``grad_input_field`` is for example ``grad_input_1``, which is a key into a grads
        dictionary.

        The method computes the gradient w.r.t. the tokens, finds the token with the maximum
        gradient (by L2 norm), and replaces it with another token based on the first-order Taylor
        approximation of the loss.  This process is iteratively repeated until the prediction
        changes.  Once a token is replaced, it is not flipped again.
        """
        if self.token_embedding is None:
            self.initialize()
        ignore_tokens = ["@@NULL@@", '.', ',', ';', '!', '?'] if ignore_tokens is None else ignore_tokens
        original_instances = self.predictor.json_to_labeled_instances(inputs)
        original_text_field: TextField = original_instances[0][input_field_to_attack]  # type: ignore
        original_tokens = deepcopy(original_text_field.tokens)
        final_tokens = []
        for i, current_instance in enumerate(original_instances):
            # Gets a list of the fields that we want to check to see if they change.
            fields_to_compare = utils.get_fields_to_compare(inputs, current_instance, input_field_to_attack)
            current_text_field: TextField = current_instance[input_field_to_attack]  # type: ignore
            current_tokens = current_text_field.tokens
            grads, outputs = self.predictor.get_gradients([current_instance])
            if 'clusters' in outputs:
                for key, output in outputs.items():
                    if isinstance(output, torch.Tensor):
                        outputs[key] = output.detach().cpu().numpy().squeeze()
                    elif isinstance(output, list):
                        outputs[key] = output[0]
                original_outputs = deepcopy(outputs)

            # ignore any token that is in the ignore_tokens list by setting the token to already flipped
            flipped: List[int] = []
            for index, token in enumerate(current_tokens):
                if token.text in ignore_tokens:
                    flipped.append(index)
            while True:
                # Compute L2 norm of all grads.
                grad = grads[grad_input_field]
                grads_magnitude = [g.dot(g) for g in grad]

                # only flip a token once
                for index in flipped:
                    grads_magnitude[index] = -1
                if 'clusters' in outputs:
                    for clust in outputs['clusters']:
                        for mention in clust:
                            for index in range(mention[0], mention[1]+1):
                                grads_magnitude[index] = -1

                # we flip the token with highest gradient norm
                index_of_token_to_flip = numpy.argmax(grads_magnitude)
                # when we have already flipped all the tokens once
                if grads_magnitude[index_of_token_to_flip] == -1:
                    break
                flipped.append(index_of_token_to_flip)

                # Get new token using taylor approximation
                input_tokens = current_text_field._indexed_tokens[self.namespace]
                original_id_of_token_to_flip = input_tokens[index_of_token_to_flip]
                new_id_of_flipped_token = self._first_order_taylor(grad[index_of_token_to_flip],
                                                              self.token_embedding.weight,  # type: ignore
                                                              original_id_of_token_to_flip,
                                                              -1)
                # flip token
                new_token = Token(self.vocab._index_to_token[self.namespace][new_id_of_flipped_token])  # type: ignore
                current_text_field.tokens[index_of_token_to_flip] = new_token
                current_instance.indexed = False

                # Get model predictions on current_instance, and then label the instances
                grads, outputs = self.predictor.get_gradients([current_instance])  # predictions
                for key, output in outputs.items():
                    if isinstance(output, torch.Tensor):
                        outputs[key] = output.detach().cpu().numpy().squeeze()
                    elif isinstance(output, list):
                        outputs[key] = output[0]

                # add labels to current_instances
                current_instance_labeled = self.predictor.predictions_to_labeled_instances(current_instance, outputs)[0]
                if 'clusters' in outputs:
                    if set(tuple(l) for l in original_outputs['clusters']) != set(tuple(l) for l in outputs['clusters']):
                        break
                # if the prediction has changed, then stop
                elif utils.instance_has_changed(current_instance_labeled, fields_to_compare):
                    break

            final_tokens.append(current_tokens)
            if 'clusters' in outputs:
                break
        return sanitize({"final": final_tokens,
                         "original": original_tokens,
                         "outputs": outputs})

    def targeted_attack_from_json(self,
                                  inputs: JsonDict = None,
                                  input_field_to_attack: str = 'tokens',
                                  grad_input_field: str = 'grad_input_1',
                                  ignore_tokens: List[str] = None,
                                  target: str = None,
                                  target_word: str = None) -> JsonDict:
        """
        Replaces one token at a time from the input until the model's prediction changes.
        ``input_field_to_attack`` is for example ``tokens``, it says what the input field is
        called.  ``grad_input_field`` is for example ``grad_input_1``, which is a key into a grads
        dictionary.

        The method computes the gradient w.r.t. the tokens, finds the token with the maximum
        gradient (by L2 norm), and replaces it with another token based on the first-order Taylor
        approximation of the loss.  This process is iteratively repeated until the prediction
        changes.  Once a token is replaced, it is not flipped again.
        """
        if self.token_embedding is None:
            self.initialize()
        if target is None:
            raise ValueError("set the target")
        ignore_tokens = ["@@NULL@@", '.', ',', ';', '!', '?'] if ignore_tokens is None else ignore_tokens
        sign = 1.0
        output_dict = {'words': [target]}
        instance = self.predictor._json_to_instance(inputs)
        original_instances = self.predictor.predictions_to_labeled_instances(instance, output_dict)
        original_text_field: TextField = original_instances[0][input_field_to_attack]  # type: ignore
        original_tokens = deepcopy(original_text_field.tokens)
        final_tokens = []
        for i, current_instance in enumerate(original_instances):
            # Gets a list of the fields that we want to check to see if they change.
            fields_to_compare = utils.get_fields_to_compare(inputs, current_instance, input_field_to_attack)
            current_text_field: TextField = current_instance[input_field_to_attack]  # type: ignore
            current_tokens = current_text_field.tokens
            grads, outputs = self.predictor.get_gradients([current_instance])
            if 'clusters' in outputs:
                for key, output in outputs.items():
                    if isinstance(output, torch.Tensor):
                        outputs[key] = output.detach().cpu().numpy().squeeze()
                    elif isinstance(output, list):
                        outputs[key] = output[0]
                original_outputs = deepcopy(outputs)

            # ignore any token that is in the ignore_tokens list by setting the token to already flipped
            flipped: List[int] = []
            if target_word is not None:
                for index, token in enumerate(current_tokens):
                    if token.text != target_word:
                        flipped.append(index)
            else:
                for index, token in enumerate(current_tokens):
                    if token.text in ignore_tokens:
                        flipped.append(index)
            while True:
                # Compute L2 norm of all grads.
                grad = grads[grad_input_field]
                grads_magnitude = [g.dot(g) for g in grad]

                # only flip a token once
                for index in flipped:
                    grads_magnitude[index] = -1
                if 'clusters' in outputs:
                    for clust in outputs['clusters']:
                        for mention in clust:
                            for index in range(mention[0], mention[1]+1):
                                grads_magnitude[index] = -1

                # we flip the token with highest gradient norm
                index_of_token_to_flip = numpy.argmax(grads_magnitude)
                # when we have already flipped all the tokens once
                if grads_magnitude[index_of_token_to_flip] == -1:
                    break
                flipped.append(index_of_token_to_flip)

                # Get new token using taylor approximation
                input_tokens = current_text_field._indexed_tokens[self.namespace]
                original_id_of_token_to_flip = input_tokens[index_of_token_to_flip]
                new_id_of_flipped_token = self._first_order_taylor(grad[index_of_token_to_flip],
                                                              self.token_embedding.weight,  # type: ignore
                                                              original_id_of_token_to_flip,
                                                              sign)
                # flip token
                new_token = Token(self.vocab._index_to_token[self.namespace][new_id_of_flipped_token])  # type: ignore
                current_text_field.tokens[index_of_token_to_flip] = new_token
                current_instance.indexed = False

                # Get model predictions on current_instance, and then label the instances
                grads, outputs = self.predictor.get_gradients([current_instance])  # predictions
                for key, output in outputs.items():
                    if isinstance(output, torch.Tensor):
                        outputs[key] = output.detach().cpu().numpy().squeeze()
                    elif isinstance(output, list):
                        outputs[key] = output[0]

                # add labels to current_instances
                current_instance_labeled = self.predictor.predictions_to_labeled_instances(current_instance, outputs)[0]
                if 'clusters' in outputs:
                    if set(tuple(l) for l in original_outputs['clusters']) != set(tuple(l) for l in outputs['clusters']):
                        break
                # if the prediction has changed, then stop
                if not utils.instance_has_changed(current_instance_labeled, fields_to_compare):
                        break

            final_tokens.append(current_tokens)
            if 'clusters' in outputs:
                break
        return sanitize({"final": final_tokens,
                         "original": original_tokens,
                         "outputs": outputs})

    def _first_order_taylor(self, grad: numpy.ndarray,
                            embedding_matrix: torch.nn.parameter.Parameter,
                            token_idx: int,
                            sign: int) -> int:
        """
        The below code is based on
        https://github.com/pmichel31415/translate/blob/paul/pytorch_translate/
        research/adversarial/adversaries/brute_force_adversary.py

        Replaces the current token_idx with another token_idx to increase the loss. In particular, this
        function uses the grad, alongside the embedding_matrix to select the token that maximizes the
        first-order taylor approximation of the loss.
        """
        grad = torch.from_numpy(grad)
        embedding_matrix = embedding_matrix.cpu()
        word_embeds = torch.nn.functional.embedding(torch.LongTensor([token_idx]),
                                                    embedding_matrix)
        word_embeds = word_embeds.detach().unsqueeze(0)
        grad = grad.unsqueeze(0).unsqueeze(0)
        # solves equation (3) here https://arxiv.org/abs/1903.06620
        new_embed_dot_grad = torch.einsum("bij,kj->bik", (grad, embedding_matrix))
        prev_embed_dot_grad = torch.einsum("bij,bij->bi", (grad, word_embeds)).unsqueeze(-1)
        neg_dir_dot_grad = sign * (prev_embed_dot_grad - new_embed_dot_grad)        
        neg_dir_dot_grad = neg_dir_dot_grad.detach().cpu().numpy()
        # Do not replace with non-alphanumeric tokens
        neg_dir_dot_grad[:,:,self.invalid_replacement_indices] = -numpy.inf
        best_at_each_step = neg_dir_dot_grad.argmax(2)
        return best_at_each_step[0].data[0]
